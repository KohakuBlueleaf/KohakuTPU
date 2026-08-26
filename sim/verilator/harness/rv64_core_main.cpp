// A card-shaped harness for rv64_core: sparse memory, an ELF loader, a console,
// and a run loop. No Verilog testbench anywhere.
//
// MEMORY IS A C++ MAP, NOT A VERILOG ARRAY, and that is the whole reason a
// serious program is possible here: an array of the address space cannot
// elaborate, while a page map costs only what the program touches.
//
// The core reads synchronously -- an address presented in one cycle is answered
// in the next -- so this models exactly that and nothing more.

#include "Vrv64_core.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>

static const uint64_t CONSOLE = 0x1000'0000ull;   // a store here is a putchar
static const uint64_t EXITREG = 0x1000'0008ull;   // a store here ends the run

static VerilatedContext *ctx;
static Vrv64_core *dut;
static std::unordered_map<uint64_t, uint64_t> mem;   // by 8-byte word index
static bool done = false;
static int exit_code = 0;

static uint64_t rd64(uint64_t a) {
    auto it = mem.find(a >> 3);
    return it == mem.end() ? 0 : it->second;
}

static void wr64(uint64_t a, uint64_t d, uint8_t strb) {
    if (a == CONSOLE) {
        putchar((int)(d & 0xff));
        fflush(stdout);
        return;
    }
    if (a == EXITREG) {
        done = true;
        exit_code = (int)(d & 0xff);
        return;
    }
    uint64_t &w = mem[a >> 3];
    for (int i = 0; i < 8; ++i)
        if (strb & (1 << i)) {
            uint64_t m = 0xffull << (i * 8);
            w = (w & ~m) | (d & m);
        }
}

static uint32_t rd32(uint64_t a) {
    uint64_t w = rd64(a & ~7ull);
    return (uint32_t)((a & 4) ? (w >> 32) : w);
}

static void wr_bytes(uint64_t a, const uint8_t *p, size_t n) {
    for (size_t i = 0; i < n; ++i) {
        uint64_t addr = a + i;
        uint64_t &w = mem[addr >> 3];
        int b = (int)(addr & 7);
        uint64_t m = 0xffull << (b * 8);
        w = (w & ~m) | ((uint64_t)p[i] << (b * 8));
    }
}

// ---- ELF64 little-endian, PT_LOAD segments only --------------------------
static bool load_elf(const char *path, uint64_t *entry) {
    FILE *f = fopen(path, "rb");
    if (!f) {
        printf("  cannot open %s\n", path);
        return false;
    }
    std::vector<uint8_t> b;
    fseek(f, 0, SEEK_END);
    b.resize(ftell(f));
    fseek(f, 0, SEEK_SET);
    if (fread(b.data(), 1, b.size(), f) != b.size()) {
        fclose(f);
        return false;
    }
    fclose(f);

    if (b.size() < 64 || memcmp(b.data(), "\x7f" "ELF", 4) || b[4] != 2) {
        printf("  not an ELF64: %s\n", path);
        return false;
    }
    auto u16 = [&](size_t o) { return (uint16_t)(b[o] | (b[o + 1] << 8)); };
    auto u64 = [&](size_t o) {
        uint64_t v = 0;
        for (int i = 7; i >= 0; --i) v = (v << 8) | b[o + i];
        return v;
    };

    *entry = u64(24);
    uint64_t phoff = u64(32);
    uint16_t phentsize = u16(54), phnum = u16(56);

    int loaded = 0;
    for (int i = 0; i < phnum; ++i) {
        size_t p = phoff + (size_t)i * phentsize;
        uint32_t type = (uint32_t)(b[p] | (b[p + 1] << 8) | (b[p + 2] << 16) |
                                   (b[p + 3] << 24));
        if (type != 1) continue;   // PT_LOAD
        uint64_t off = u64(p + 8), vaddr = u64(p + 16);
        uint64_t filesz = u64(p + 32), memsz = u64(p + 40);
        wr_bytes(vaddr, b.data() + off, (size_t)filesz);
        for (uint64_t z = filesz; z < memsz; ++z) {
            uint64_t addr = vaddr + z;
            uint64_t &w = mem[addr >> 3];
            w &= ~(0xffull << ((addr & 7) * 8));
        }
        ++loaded;
    }
    printf("  loaded %s: %d segment(s), entry %016llx\n", path, loaded,
           (unsigned long long)*entry);
    return loaded > 0;
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);

    const char *elf = nullptr;
    uint64_t max_cycles = 100'000'000ull;
    bool trace_retire = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--elf" && i + 1 < argc) elf = argv[++i];
        else if (a == "--max-cycles" && i + 1 < argc)
            max_cycles = strtoull(argv[++i], nullptr, 0);
        else if (a == "--trace-retire") trace_retire = true;
    }
    if (!elf) {
        printf("  usage: vsim --elf PROG.elf [--max-cycles N] [--trace-retire]\n");
        return 2;
    }

    uint64_t entry = 0;
    if (!load_elf(elf, &entry)) return 2;

    dut = new Vrv64_core(ctx);
    dut->resetn = 0;
    dut->imem_data = 0;
    dut->dmem_rdata = 0;
    dut->irq_ext = 0;
    dut->irq_soft = 0;
    dut->ext_halt = 0;
    dut->dmem_stall = 0;
    for (int i = 0; i < 4; ++i) {
        dut->clk = 0; dut->eval();
        dut->clk = 1; dut->eval();
    }
    dut->resetn = 1;

    uint64_t cycles = 0, retired = 0;
    while (cycles < max_cycles && !done && !dut->halted) {
        dut->clk = 0;
        dut->eval();

        uint64_t ia = dut->imem_addr;
        uint64_t da = dut->dmem_addr;
        uint8_t ws = dut->dmem_wstrb;
        uint64_t wd = dut->dmem_wdata;

        if (trace_retire && dut->dbg_retire)
            printf("    pc %016llx\n", (unsigned long long)dut->dbg_pc);
        if (dut->dbg_retire) ++retired;

        dut->clk = 1;
        dut->eval();
        ctx->timeInc(1);

        if (ws) wr64(da, wd, ws);
        dut->imem_data = rd32(ia);
        dut->dmem_rdata = rd64(da);
        ++cycles;
    }

    printf("\n");
    printf("  cycles     %llu\n", (unsigned long long)cycles);
    printf("  retired    %llu\n", (unsigned long long)retired);
    if (cycles)
        printf("  IPC        %.4f   (stall %.2f%%)\n",
               (double)retired / (double)cycles,
               100.0 * (double)(cycles - retired) / (double)cycles);
    if (dut->halted)
        printf("  halted     cause %u at pc %016llx\n", (unsigned)dut->halt_cause,
               (unsigned long long)dut->halt_pc);
    if (done) printf("  exit       %d\n", exit_code);
    if (cycles >= max_cycles) printf("  TIMEOUT\n");

    bool ok = (done && exit_code == 0) ||
              (dut->halted && dut->halt_cause == 1);
    printf("========================================\n");
    printf("  %s\n", ok ? "PASS" : "FAIL");
    printf("========================================\n");

    dut->final();
    delete dut;
    delete ctx;
    return ok ? 0 : 1;
}
