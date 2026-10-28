// rv64_node_pair with a per-cycle trace of node A's load path while it runs
// in supervisor mode: the core's request, the MMU's answer and the memory
// stage, one line per cycle. Built with --public-flat-rw; loads, boots and
// finishes exactly as sim/verilator/harness/rv64_node_pair_main.cpp.
// Used by software/os/tests/xlat (the Sv39 load reproduction).

#include "Vrv64_node_pair.h"
#include "Vrv64_node_pair___024root.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <map>
#include <string>
#include <vector>

static const uint32_t H_IMEM = 0x0000'0000u;
static const uint32_t H_SPAD = 0x1000'0000u;
static const uint32_t H_CTRL = 0x2000'0000u;
static const uint8_t HR_BOOT = 0x00, HR_PC = 0x08, HR_STATUS = 0x18, HR_EXIT = 0x20;
static const uint64_t SPAD_BASE = 0x0001'0000ull;

double sc_time_stamp() { return 0.0; }

static VerilatedContext *ctx;
static Vrv64_node_pair *dut;
static uint64_t cycles = 0;
static std::string console[2];
static long trace_left = 6000;

#define A(s) (r->rv64_node_pair__DOT__node__BRA__0__KET____DOT__u__DOT__u_pe__DOT__u_cpu__DOT__##s)

static void trace() {
    Vrv64_node_pair___024root *r = dut->rootp;
    if (trace_left <= 0 || A(core_priv) != 1) return;
    --trace_left;
    printf("T %8llu pc %5llx ev%d req%d va %10llx busy%d rq%d same%d qoff %3llx pa %10llx "
           "first%d st%d done%d mpa %10llx path%d rdata %16llx | wb x%-2d we%d %16llx\n",
           (unsigned long long)cycles, (unsigned long long)A(u_core__DOT__e_pc),
           (int)A(u_core__DOT__e_valid), (int)A(dmem_req_c), (unsigned long long)A(dmem_addr_c),
           (int)A(mmu_busy), (int)A(u_mmu__DOT__resolved_q), (int)A(u_mmu__DOT__same_req),
           (unsigned long long)A(u_mmu__DOT__q_off), (unsigned long long)A(pa), (int)A(mem_first),
           (int)A(mem_started), (int)A(mem_done), (unsigned long long)A(m_pa_q), (int)A(path_q),
           (unsigned long long)A(dmem_rdata_c), (int)A(u_core__DOT__wb_rd),
           (int)A(u_core__DOT__wb_we), (unsigned long long)A(u_core__DOT__wb_val));
}

static void tick() {
    dut->clk = 0;
    dut->eval();
    trace();
    if (dut->a_console_we) console[0].push_back((char)dut->a_console);
    if (dut->b_console_we) console[1].push_back((char)dut->b_console);
    dut->clk = 1;
    dut->eval();
    ctx->timeInc(1);
    ++cycles;
}

static void hwrite(int n, uint32_t addr, uint64_t data, uint8_t strb = 0xff) {
    if (n == 0) {
        dut->a_hs_addr = addr; dut->a_hs_wdata = data;
        dut->a_hs_wstrb = strb; dut->a_hs_wr = 1;
        tick();
        dut->a_hs_wr = 0; dut->a_hs_wstrb = 0;
    } else {
        dut->b_hs_addr = addr; dut->b_hs_wdata = data;
        dut->b_hs_wstrb = strb; dut->b_hs_wr = 1;
        tick();
        dut->b_hs_wr = 0; dut->b_hs_wstrb = 0;
    }
}

static uint64_t hread(int n, uint32_t addr) {
    if (n == 0) {
        dut->a_hs_addr = addr; dut->a_hs_rd = 1;
        tick();
        dut->a_hs_rd = 0;
        return dut->a_hs_rdata;
    }
    dut->b_hs_addr = addr; dut->b_hs_rd = 1;
    tick();
    dut->b_hs_rd = 0;
    return dut->b_hs_rdata;
}

struct Image {
    std::map<uint64_t, uint8_t> bytes;
    uint64_t entry = 0;
    uint8_t at(uint64_t a) const {
        auto it = bytes.find(a);
        return it == bytes.end() ? 0 : it->second;
    }
};

static bool load_elf(const char *path, Image &im) {
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
    im.entry = u64(24);
    uint64_t phoff = u64(32);
    uint16_t phes = u16(54), phnum = u16(56);
    for (int i = 0; i < phnum; ++i) {
        size_t p = phoff + (size_t)i * phes;
        uint32_t ty = (uint32_t)(b[p] | (b[p+1] << 8) | (b[p+2] << 16) | (b[p+3] << 24));
        if (ty != 1) continue;
        uint64_t off = u64(p+8), va = u64(p+16), fsz = u64(p+32), msz = u64(p+40);
        for (uint64_t k = 0; k < fsz; ++k) im.bytes[va + k] = b[off + k];
        for (uint64_t k = fsz; k < msz; ++k) im.bytes[va + k] = 0;
    }
    return true;
}

static void load(int n, const Image &im) {
    uint64_t text_bytes = 0, spad_bytes = 0;
    for (auto &kv : im.bytes) {
        if (kv.first < SPAD_BASE) { if (kv.first + 1 > text_bytes) text_bytes = kv.first + 1; }
        else { uint64_t o = kv.first - SPAD_BASE;
               if (o + 1 > spad_bytes) spad_bytes = o + 1; }
    }
    for (uint64_t a = 0; a < text_bytes; a += 4) {
        uint32_t w = 0;
        for (int j = 0; j < 4; ++j) w |= (uint32_t)im.at(a + j) << (8 * j);
        hwrite(n, H_IMEM | (uint32_t)a, w);
    }
    for (uint64_t a = 0; a < spad_bytes; a += 8) {
        uint64_t w = 0;
        for (int j = 0; j < 8; ++j) w |= (uint64_t)im.at(SPAD_BASE + a + j) << (8 * j);
        hwrite(n, H_SPAD | (uint32_t)a, w);
    }
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    const char *elf[2] = {nullptr, nullptr};
    uint64_t max_cycles = 4000000;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--elf-a" && i + 1 < argc) elf[0] = argv[++i];
        else if (a == "--elf-b" && i + 1 < argc) elf[1] = argv[++i];
        else if (a == "--max-cycles" && i + 1 < argc) max_cycles = strtoull(argv[++i], nullptr, 0);
        else if (a == "--trace-lines" && i + 1 < argc) trace_left = strtol(argv[++i], nullptr, 0);
    }
    if (!elf[0] || !elf[1]) { printf("  --elf-a and --elf-b are required\n"); return 2; }
    Image im[2];
    if (!load_elf(elf[0], im[0]) || !load_elf(elf[1], im[1])) return 2;

    dut = new Vrv64_node_pair(ctx);
    dut->resetn = 0;
    dut->a_hs_wr = dut->a_hs_rd = 0; dut->a_hs_wstrb = 0;
    dut->b_hs_wr = dut->b_hs_rd = 0; dut->b_hs_wstrb = 0;
    for (int i = 0; i < 16; ++i) tick();
    dut->resetn = 1;
    for (int i = 0; i < 200; ++i) tick();
    load(0, im[0]);
    load(1, im[1]);
    hwrite(1, H_CTRL | HR_PC, im[1].entry);
    hwrite(1, H_CTRL | HR_BOOT, 1);
    hwrite(0, H_CTRL | HR_PC, im[0].entry);
    hwrite(0, H_CTRL | HR_BOOT, 1);

    bool done[2] = {false, false};
    while (cycles < max_cycles && !(done[0] && done[1])) {
        for (int i = 0; i < 512; ++i) tick();
        for (int n = 0; n < 2; ++n)
            if (!done[n] && (hread(n, H_CTRL | HR_STATUS) & 0x8)) done[n] = true;
    }
    for (int n = 0; n < 2; ++n) {
        printf("node %c exit %llu%s\n%s", 'A' + n, (unsigned long long)hread(n, H_CTRL | HR_EXIT),
               done[n] ? "" : " (never finished)", console[n].c_str());
    }
    printf("total %llu cycles\n", (unsigned long long)cycles);
    dut->final();
    delete dut;
    delete ctx;
    return 0;
}
