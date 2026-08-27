// Drives rv64_win_2p2 through its compact 4 KB control window (lb_* register
// bus, no AXI): stream the program into imem/spad via LOAD_CTL/LOAD_DATA, boot,
// poll STATUS, drain the console FIFO. This is the tidy per-node load path the
// station bus would deliver.

#include "Vrv64_win_2p2.h"
#include "verilated.h"

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

// window offsets (byte, inside the 4 KB slot)
static const uint32_t W_BOOT = 0x00, W_STATUS = 0x18, W_EXIT = 0x20;
static const uint32_t W_STDIN = 0x40, W_LOADC = 0x80, W_LOADD = 0x88, W_CONS = 0x90;
static const uint64_t SPAD_BASE = 0x0001'0000ull;

static VerilatedContext *ctx;
static Vrv64_win_2p2 *dut;
static uint64_t cycles = 0;
static std::string console;
static std::string sideband;

static void tick() {
    static const int cs[4] = {0, 0, 1, 1};
    static const int c2[4] = {0, 1, 0, 1};
    for (int p = 0; p < 4; ++p) {
        dut->clk   = cs[p];
        dut->clk2x = c2[p];
        dut->eval();
        if (dut->con_we_o) sideband.push_back((char)dut->con_o);
    }
    ctx->timeInc(1);
    ++cycles;
}

static void lb_write(uint32_t addr, uint64_t data, uint8_t strb = 0xff) {
    dut->lb_addr = addr;
    dut->lb_wdata = data;
    dut->lb_wstrb = strb;
    dut->lb_wr = 1;
    dut->lb_en = 1;
    tick();
    dut->lb_en = 0;
    dut->lb_wr = 0;
}

static uint64_t lb_read(uint32_t addr) {
    dut->lb_addr = addr;
    dut->lb_wr = 0;
    dut->lb_en = 1;
    tick();
    uint64_t v = dut->lb_rdata;
    dut->lb_en = 0;
    return v;
}

// Drain whatever the program has printed into the console FIFO.
static void drain_console() {
    uint64_t h = lb_read(W_CONS);
    while (h & 0x100) {
        console.push_back((char)(h & 0xff));
        lb_write(W_CONS, 0);            // pop
        h = lb_read(W_CONS);
    }
}

// ---- the image -------------------------------------------------------------
static std::map<uint64_t, uint8_t> image;
static uint64_t entry_pc = 0;

static bool load_elf(const char *path, std::map<uint64_t, uint8_t> &img_map,
                     uint64_t &entry) {
    FILE *f = fopen(path, "rb");
    if (!f) { printf("  cannot open %s\n", path); return false; }
    std::vector<uint8_t> b;
    fseek(f, 0, SEEK_END); b.resize(ftell(f)); fseek(f, 0, SEEK_SET);
    if (fread(b.data(), 1, b.size(), f) != b.size()) { fclose(f); return false; }
    fclose(f);
    auto u16 = [&](size_t o) { return (uint16_t)(b[o] | (b[o+1] << 8)); };
    auto u64 = [&](size_t o) {
        uint64_t v = 0; for (int i = 7; i >= 0; --i) v = (v << 8) | b[o+i];
        return v;
    };
    entry = u64(24);
    uint64_t phoff = u64(32);
    uint16_t phes = u16(54), phnum = u16(56);
    for (int i = 0; i < phnum; ++i) {
        size_t p = phoff + (size_t)i * phes;
        uint32_t ty = (uint32_t)(b[p] | (b[p+1] << 8) | (b[p+2] << 16) | (b[p+3] << 24));
        if (ty != 1) continue;
        uint64_t off = u64(p+8), va = u64(p+16), fsz = u64(p+32), msz = u64(p+40);
        for (uint64_t k = 0; k < fsz; ++k) img_map[va + k] = b[off + k];
        for (uint64_t k = fsz; k < msz; ++k) img_map[va + k] = 0;
    }
    return true;
}

static uint8_t img(uint64_t a) {
    auto it = image.find(a);
    return it == image.end() ? 0 : it->second;
}

// Place a DRAM-resident program into axi_ram the way the host would over
// S_AXI_MEM -- here through the backdoor, one 512-bit line at a time. va maps to
// axi_ram word (va>>6)&0x3fff, matching how the core reaches DRAM.
static void preload_dram(const std::map<uint64_t, uint8_t> &dimg) {
    std::map<uint32_t, std::array<uint8_t, 64>> lines;
    for (auto &kv : dimg) {
        if (kv.first < 0x80000000ull) continue;
        uint32_t word = (uint32_t)((kv.first >> 6) & 0x3fff);
        lines.emplace(word, std::array<uint8_t, 64>{});
        lines[word][kv.first & 63] = kv.second;
    }
    for (auto &kv : lines) {
        for (int k = 0; k < 16; ++k) {
            uint32_t w = 0;
            for (int b = 0; b < 4; ++b) w |= (uint32_t)kv.second[k * 4 + b] << (8 * b);
            dut->bd_wdata[k] = w;
        }
        dut->bd_addr = kv.first;
        dut->bd_we = 1;
        tick();
        dut->bd_we = 0;
    }
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    const char *elf = nullptr, *dram_elf = nullptr;
    uint64_t max_cycles = 20000000;
    std::string expect = "mesh ok";
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--elf" && i + 1 < argc) elf = argv[++i];
        else if (a == "--dram-elf" && i + 1 < argc) dram_elf = argv[++i];
        else if (a == "--max-cycles" && i + 1 < argc)
            max_cycles = strtoull(argv[++i], nullptr, 0);
        else if (a == "--expect" && i + 1 < argc) expect = argv[++i];
    }
    if (!elf) { printf("  --elf is required\n"); return 2; }
    if (!load_elf(elf, image, entry_pc)) return 2;
    std::map<uint64_t, uint8_t> dram_image;
    uint64_t dram_entry = 0;
    if (dram_elf && !load_elf(dram_elf, dram_image, dram_entry)) return 2;

    uint64_t text_bytes = 0, spad_bytes = 0;
    for (auto &kv : image) {
        if (kv.first < SPAD_BASE) { if (kv.first + 1 > text_bytes) text_bytes = kv.first + 1; }
        else if (kv.first < 0x80000000ull) { uint64_t o = kv.first - SPAD_BASE;
               if (o + 1 > spad_bytes) spad_bytes = o + 1; }
    }

    dut = new Vrv64_win_2p2(ctx);
    dut->resetn = 0;
    dut->clk = 0;
    dut->clk2x = 0;
    dut->lb_en = 0;
    dut->lb_wr = 0;
    dut->bd_we = 0;
    dut->bd_addr = 0;
    for (int i = 0; i < 8; ++i) tick();
    dut->resetn = 1;
    for (int i = 0; i < 4; ++i) tick();

    // The "large" program body, placed in DRAM before boot (as the host would).
    if (dram_elf) { preload_dram(dram_image); }

    // Stream the imem bootstrap in through the window: imem, then spad.
    lb_write(W_LOADC, 0);                       // region 0 = imem, start 0
    for (uint64_t a = 0; a < text_bytes; a += 4) {
        uint32_t w = 0;
        for (int j = 0; j < 4; ++j) w |= (uint32_t)img(a + j) << (8 * j);
        lb_write(W_LOADD, w);
    }
    lb_write(W_LOADC, 1);                        // region 1 = spad, start 0
    for (uint64_t a = 0; a < spad_bytes; a += 8) {
        uint64_t w = 0;
        for (int j = 0; j < 8; ++j) w |= (uint64_t)img(SPAD_BASE + a + j) << (8 * j);
        lb_write(W_LOADD, w);
    }
    uint64_t load_cycles = cycles;

    lb_write(W_BOOT, 1);

    uint64_t st = 0;
    while (cycles < max_cycles) {
        for (int i = 0; i < 64; ++i) tick();
        drain_console();
        st = lb_read(W_STATUS);
        if (st & 0xc) break;
    }
    drain_console();
    uint64_t exit_word = lb_read(W_EXIT);

    printf("\n");
    if (!console.empty()) printf("  console    %s", console.c_str());
    if (console.empty() || console.back() != '\n') printf("\n");
    printf("  load       %llu cycles (streamed through the 4 KB window)\n",
           (unsigned long long)load_cycles);
    printf("  text %llu bytes, spad %llu bytes\n",
           (unsigned long long)text_bytes, (unsigned long long)spad_bytes);
    printf("  exit       %llu\n", (unsigned long long)exit_word);

    bool ok;
    if (dram_elf) {
        // The DRAM program returned its result as the exit word.
        printf("  status %llx  haltpc %llx\n",
               (unsigned long long)st, (unsigned long long)lb_read(0x28));
        printf("  dram exit-word %llu (want 98176)  bootloader console %s\n",
               (unsigned long long)exit_word, console.c_str());
        ok = (exit_word == 98176) && console.find("boot") != std::string::npos;
    }
    else {
        ok = (exit_word == 0) && console.find(expect) != std::string::npos;
    }
    printf("========================================\n");
    printf("  %s\n", ok ? "PASS" : "FAIL");
    printf("========================================\n");

    dut->final();
    delete dut;
    delete ctx;
    return ok ? 0 : 1;
}
