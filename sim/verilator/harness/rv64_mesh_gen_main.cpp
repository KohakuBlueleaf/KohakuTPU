// Harness for rv64_mesh_2p2: the whole mesh with an internal axi_ram DRAM and
// real matmul/vector units. The host loads the RV64 over the hs_ window exactly
// as the standalone syscore bench does, then lets it run; there is no C++ node
// or unit model here -- DRAM is the axi_ram, the units are RTL, and the router
// carries the dispatch. The DRAM backdoor lets us preload operands and read
// results back by 512-bit word.

#include "Vrv64_gen_2p2.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

static const uint32_t H_IMEM = 0x0000'0000u;
static const uint32_t H_SPAD = 0x1000'0000u;
static const uint32_t H_CTRL = 0x2000'0000u;
static const uint8_t  HR_BOOT = 0x00, HR_PC = 0x08, HR_STATUS = 0x18,
                      HR_EXIT = 0x20, HR_HALTPC = 0x28;
static const uint64_t SPAD_BASE = 0x0001'0000ull;

static VerilatedContext *ctx;
static Vrv64_gen_2p2 *dut;
static uint64_t cycles = 0;
static std::string console;

static void tick() {
    // Two clocks: clk (the fabric/MAG/unit rate) and clk2x = 2x clk for the
    // matmul L1+DSP pump. clk: 0,0,1,1 ; clk2x: 0,1,0,1 -- clk posedge at phase
    // 2, two clk2x posedges per clk period, edges aligned as BUFGCE_DIV would.
    static const int cs[4] = {0, 0, 1, 1};
    static const int c2[4] = {0, 1, 0, 1};
    for (int p = 0; p < 4; ++p) {
        dut->clk   = cs[p];
        dut->clk2x = c2[p];
        dut->eval();
    }
    if (dut->hs_console_we) console.push_back((char)dut->hs_console);
    ctx->timeInc(1);
    ++cycles;
}

static void hwrite(uint32_t addr, uint64_t data, uint8_t strb = 0xff) {
    dut->hs_addr = addr;
    dut->hs_wdata = data;
    dut->hs_wstrb = strb;
    dut->hs_wr = 1;
    tick();
    dut->hs_wr = 0;
    dut->hs_wstrb = 0;
}

static uint64_t hread(uint32_t addr) {
    dut->hs_addr = addr;
    dut->hs_rd = 1;
    tick();
    dut->hs_rd = 0;
    return dut->hs_rdata;
}

// A 512-bit DRAM line by byte address, through the backdoor. Combinational read,
// so eval after setting the word index and grab the lane we want.
static uint64_t dram_read8(uint64_t byte_addr) {
    uint32_t word = (uint32_t)((byte_addr >> 6) & 0x3fff);
    int lane = (int)((byte_addr >> 3) & 7);
    dut->bd_addr = word;
    dut->eval();
    return ((uint64_t)dut->bd_rdata[2 * lane + 1] << 32) | dut->bd_rdata[2 * lane];
}

// ---- the image -------------------------------------------------------------
static std::map<uint64_t, uint8_t> image;
static uint64_t entry_pc = 0;

static bool load_elf(const char *path) {
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
    entry_pc = u64(24);
    uint64_t phoff = u64(32);
    uint16_t phes = u16(54), phnum = u16(56);
    for (int i = 0; i < phnum; ++i) {
        size_t p = phoff + (size_t)i * phes;
        uint32_t ty = (uint32_t)(b[p] | (b[p+1] << 8) | (b[p+2] << 16) | (b[p+3] << 24));
        if (ty != 1) continue;
        uint64_t off = u64(p+8), va = u64(p+16), fsz = u64(p+32), msz = u64(p+40);
        for (uint64_t k = 0; k < fsz; ++k) image[va + k] = b[off + k];
        for (uint64_t k = fsz; k < msz; ++k) image[va + k] = 0;
    }
    return true;
}

static uint8_t img(uint64_t a) {
    auto it = image.find(a);
    return it == image.end() ? 0 : it->second;
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    const char *elf = nullptr;
    uint64_t max_cycles = 20000000;
    std::string expect = "mesh ok";
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--elf" && i + 1 < argc) elf = argv[++i];
        else if (a == "--max-cycles" && i + 1 < argc)
            max_cycles = strtoull(argv[++i], nullptr, 0);
        else if (a == "--expect" && i + 1 < argc) expect = argv[++i];
    }
    if (!elf) { printf("  --elf is required\n"); return 2; }
    if (!load_elf(elf)) return 2;

    uint64_t text_bytes = 0, spad_bytes = 0;
    for (auto &kv : image) {
        if (kv.first < SPAD_BASE) { if (kv.first + 1 > text_bytes) text_bytes = kv.first + 1; }
        else if (kv.first < 0x80000000ull) { uint64_t o = kv.first - SPAD_BASE;
               if (o + 1 > spad_bytes) spad_bytes = o + 1; }
    }
    printf("  text %llu bytes, spad %llu bytes\n",
           (unsigned long long)text_bytes, (unsigned long long)spad_bytes);

    dut = new Vrv64_gen_2p2(ctx);
    dut->resetn = 0;
    dut->clk = 0;
    dut->clk2x = 0;
    dut->hs_wr = dut->hs_rd = 0;
    dut->hs_wstrb = 0;
    dut->bd_we = 0;
    dut->bd_addr = 0;
    for (int i = 0; i < 8; ++i) tick();
    dut->resetn = 1;
    for (int i = 0; i < 4; ++i) tick();

    for (uint64_t a = 0; a < text_bytes; a += 4) {
        uint32_t w = 0;
        for (int j = 0; j < 4; ++j) w |= (uint32_t)img(a + j) << (8 * j);
        hwrite(H_IMEM | (uint32_t)a, w);
    }
    for (uint64_t a = 0; a < spad_bytes; a += 8) {
        uint64_t w = 0;
        for (int j = 0; j < 8; ++j) w |= (uint64_t)img(SPAD_BASE + a + j) << (8 * j);
        hwrite(H_SPAD | (uint32_t)a, w);
    }
    uint64_t load_cycles = cycles;

    hwrite(H_CTRL | HR_PC, entry_pc);
    hwrite(H_CTRL | HR_BOOT, 1);

    uint64_t run0 = cycles, st = 0;
    while (cycles < max_cycles) {
        for (int i = 0; i < 256; ++i) tick();
        st = hread(H_CTRL | HR_STATUS);
        if (st & 0xc) break;
    }

    uint64_t exit_word = hread(H_CTRL | HR_EXIT);
    uint64_t halt_pc = hread(H_CTRL | HR_HALTPC);
    if (st & 0x4)
        printf("  HALTED     cause %llu at pc %llx\n",
               (unsigned long long)(st & 3), (unsigned long long)halt_pc);
    printf("\n");
    if (!console.empty()) printf("  console    %s", console.c_str());
    if (console.empty() || console.back() != '\n') printf("\n");
    printf("  load       %llu cycles\n", (unsigned long long)load_cycles);
    printf("  run        %llu cycles\n", (unsigned long long)(cycles - run0));
    printf("  dram[0..3] %llu %llu %llu %llu\n",
           (unsigned long long)dram_read8(0x80000000ull),
           (unsigned long long)dram_read8(0x80000008ull),
           (unsigned long long)dram_read8(0x80000010ull),
           (unsigned long long)dram_read8(0x80000018ull));
    printf("  exit       %llu\n", (unsigned long long)exit_word);

    // Check the vec output straight out of axi_ram, independent of the
    // processor's cache. sum[i] is FP16, so the first four lanes are f16 of
    // 3,6,9,12. The hand-built kernel drains to 0x2200; the compiler artifact to
    // 0x3000.
    bool dram_ok = true;
    uint32_t sum_byte = 0;
    if (expect.find("vadd") != std::string::npos) { sum_byte = 0x2200; }
    else if (expect.find("art") != std::string::npos) { sum_byte = 0x3000; }
    if (sum_byte) {
        dut->bd_addr = sum_byte >> 6;
        dut->eval();
        uint32_t s01 = dut->bd_rdata[0], s23 = dut->bd_rdata[1];
        printf("  dram sum   %04x %04x %04x %04x  (want 4200 4600 4880 4a00)\n",
               s01 & 0xffff, s01 >> 16, s23 & 0xffff, s23 >> 16);
        dram_ok = (s01 == 0x46004200u) && (s23 == 0x4a004880u);
    }

    if (expect.find("matmul") != std::string::npos) {
        for (uint32_t w : {0x40u, 0x41u, 0x80u, 0x81u, 0xc0u}) {
            dut->bd_addr = w;
            dut->eval();
            printf("  ram %03x:", w);
            for (int k = 0; k < 4; ++k) printf(" %08x", dut->bd_rdata[k]);
            printf("\n");
        }
    }

    bool ok = (exit_word == 0) && dram_ok
              && console.find(expect) != std::string::npos;
    printf("========================================\n");
    printf("  %s\n", ok ? "PASS" : "FAIL");
    printf("========================================\n");

    dut->final();
    delete dut;
    delete ctx;
    return ok ? 0 : 1;
}
