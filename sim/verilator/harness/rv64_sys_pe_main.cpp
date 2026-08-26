// A node-shaped harness for rv64_sys_pe: the image goes in over CU_DATA flits,
// the kick goes in over CU_INST, and the result comes back on CU_SIGNAL.
//
// NOTHING REACHES INSIDE THE UNIT. The core harness pokes a memory model; this
// one only drives the NoC port, which is the whole point of phase 2 -- if the
// endpoint contract is wrong, this fails, and no back door hides it.
//
// Flit layout, MSB first (288 bits, POS_WIDTH 4):
//   dx[4] dy[4] sx[4] sy[4] ty[4] id[8] spare[4] payload[256]

#include "Vrv64_sys_pe.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

static const int      FLIT_WORDS = 288 / 32;      // VlWide, 32 bits per word
static const uint8_t  T_CU_INST = 0x5, T_CU_SIGNAL = 0x6, T_CU_DATA = 0x8;
static const uint8_t  BUF_SPAD = 0, BUF_IMEM = 1;
static const uint64_t SPAD_BASE = 0x00010000ull;
static const uint64_t CTRL_BASE = 0x00020000ull;

static VerilatedContext *ctx;
static Vrv64_sys_pe *dut;

// ---- a 288-bit flit as bytes, so field placement is explicit ---------------
struct Flit {
    uint8_t b[36];                                 // [0] is the MSB byte
    Flit() { memset(b, 0, sizeof b); }

    void put(int hi_bit, int width, uint64_t v) {   // hi_bit counts from bit 287
        for (int i = 0; i < width; ++i) {
            int bit = hi_bit - i;                   // 287..0
            int idx = 287 - bit;                    // 0..287 from the MSB
            uint64_t nv = (v >> (width - 1 - i)) & 1;
            if (nv) b[idx >> 3] |= (uint8_t)(0x80 >> (idx & 7));
        }
    }
    void header(uint8_t ty, uint8_t id, int dx, int dy, int sx, int sy) {
        put(287, 4, (uint64_t)dx);
        put(283, 4, (uint64_t)dy);
        put(279, 4, (uint64_t)sx);
        put(275, 4, (uint64_t)sy);
        put(271, 4, ty);
        put(267, 8, id);
    }
    // payload bit p (0..255) sits at absolute bit p
    void pay(int hi, int width, uint64_t v) { put(hi, width, v); }
};

static void drive(const Flit &f, uint32_t *w) {
    for (int i = 0; i < FLIT_WORDS; ++i) w[i] = 0;
    for (int bit = 0; bit < 288; ++bit) {
        int idx = 287 - bit;
        if (f.b[idx >> 3] & (0x80 >> (idx & 7))) w[bit >> 5] |= (1u << (bit & 31));
    }
}

static uint64_t grab(const uint32_t *w, int hi_bit, int width) {
    uint64_t v = 0;
    for (int i = 0; i < width; ++i) {
        int bit = hi_bit - i;
        v = (v << 1) | ((w[bit >> 5] >> (bit & 31)) & 1);
    }
    return v;
}

// ---- clocking --------------------------------------------------------------
static uint64_t cycles = 0;
static bool     sig_seen = false;
static uint8_t  sig_code = 0;
static uint32_t sig_arg = 0;
static std::string console;

static void tick() {
    dut->clk = 0;
    dut->eval();
    if (dut->dbg_console_we) console.push_back((char)dut->dbg_console);
    if (dut->noc_out_valid) {
        uint8_t ty = (uint8_t)grab(dut->noc_out_data, 271, 4);
        if (ty == T_CU_SIGNAL) {
            sig_code = (uint8_t)grab(dut->noc_out_data, 247, 8);
            sig_arg  = (uint32_t)grab(dut->noc_out_data, 239, 32);
            sig_seen = true;
        }
    }
    dut->clk = 1;
    dut->eval();
    ctx->timeInc(1);
    ++cycles;
}

static void send(const Flit &f) {
    drive(f, dut->noc_in_data);
    dut->noc_in_valid = 1;
    // noc_in_busy is the endpoint's backpressure; hold the flit until it clears.
    for (int guard = 0; guard < 10000; ++guard) {
        dut->clk = 0;
        dut->eval();
        bool taken = !dut->noc_in_busy;
        if (dut->dbg_console_we) console.push_back((char)dut->dbg_console);
        dut->clk = 1;
        dut->eval();
        ctx->timeInc(1);
        ++cycles;
        if (taken) break;
    }
    dut->noc_in_valid = 0;
}

// ---- the image -------------------------------------------------------------
static std::map<uint64_t, uint8_t> image;          // by byte address
static uint64_t entry_pc = 0;

static bool load_elf(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) { printf("  cannot open %s\n", path); return false; }
    std::vector<uint8_t> b;
    fseek(f, 0, SEEK_END); b.resize(ftell(f)); fseek(f, 0, SEEK_SET);
    if (fread(b.data(), 1, b.size(), f) != b.size()) { fclose(f); return false; }
    fclose(f);
    if (b.size() < 64 || memcmp(b.data(), "\x7f" "ELF", 4) || b[4] != 2) {
        printf("  not an ELF64\n"); return false;
    }
    auto u16 = [&](size_t o) { return (uint16_t)(b[o] | (b[o + 1] << 8)); };
    auto u64 = [&](size_t o) {
        uint64_t v = 0; for (int i = 7; i >= 0; --i) v = (v << 8) | b[o + i];
        return v;
    };
    entry_pc = u64(24);
    uint64_t phoff = u64(32);
    uint16_t phentsize = u16(54), phnum = u16(56);
    int loaded = 0;
    for (int i = 0; i < phnum; ++i) {
        size_t p = phoff + (size_t)i * phentsize;
        uint32_t type = (uint32_t)(b[p] | (b[p+1] << 8) | (b[p+2] << 16) |
                                   (b[p+3] << 24));
        if (type != 1) continue;
        uint64_t off = u64(p + 8), vaddr = u64(p + 16);
        uint64_t filesz = u64(p + 32), memsz = u64(p + 40);
        for (uint64_t k = 0; k < filesz; ++k) image[vaddr + k] = b[off + k];
        for (uint64_t k = filesz; k < memsz; ++k) image[vaddr + k] = 0;
        ++loaded;
    }
    printf("  loaded %s: %d segment(s), entry %llx\n", path, loaded,
           (unsigned long long)entry_pc);
    return loaded > 0;
}

static uint8_t img(uint64_t a) {
    auto it = image.find(a);
    return it == image.end() ? 0 : it->second;
}

// One CU_DATA burst: a header beat naming buf/off/len, then len+1 granules of
// 256 bits each. `off` counts GRANULES, which is what the unit's bounds check
// compares against.
static void send_region(uint8_t buf, uint64_t base, uint64_t bytes, int sx, int sy)
{
    uint64_t grans = (bytes + 31) / 32;
    if (!grans) return;
    const uint64_t CHUNK = 64;                     // len is 8 bits
    for (uint64_t g0 = 0; g0 < grans; g0 += CHUNK) {
        uint64_t n = grans - g0; if (n > CHUNK) n = CHUNK;
        Flit h;
        h.header(T_CU_DATA, 0, 2, 2, sx, sy);
        h.pay(255, 8, buf);
        h.pay(247, 16, g0);
        h.pay(231, 8, n - 1);
        send(h);
        for (uint64_t g = 0; g < n; ++g) {
            Flit d;
            d.header(T_CU_DATA, 0, 2, 2, sx, sy);
            // Byte j of the granule is the LOW byte of payload word j, so the
            // little-endian image lands the same way the core reads it.
            for (int j = 0; j < 32; ++j) {
                uint8_t v = img(base + (g0 + g) * 32 + j);
                d.pay(8 * j + 7, 8, v);
            }
            send(d);
        }
    }
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    const char *elf = nullptr;
    uint64_t max_cycles = 20000000;
    bool stress = false;
    std::string expect = "pe ok";
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--elf" && i + 1 < argc) elf = argv[++i];
        else if (a == "--max-cycles" && i + 1 < argc)
            max_cycles = strtoull(argv[++i], nullptr, 0);
        else if (a == "--stress") stress = true;
        else if (a == "--expect" && i + 1 < argc) expect = argv[++i];
    }
    if (!elf) { printf("  --elf is required\n"); return 2; }
    if (!load_elf(elf)) return 2;

    uint64_t text_bytes = 0, spad_bytes = 0;
    for (auto &kv : image) {
        if (kv.first < SPAD_BASE) {
            if (kv.first + 1 > text_bytes) text_bytes = kv.first + 1;
        } else {
            uint64_t off = kv.first - SPAD_BASE;
            if (off + 1 > spad_bytes) spad_bytes = off + 1;
        }
    }
    printf("  text %llu bytes, spad %llu bytes\n",
           (unsigned long long)text_bytes, (unsigned long long)spad_bytes);

    dut = new Vrv64_sys_pe(ctx);
    dut->resetn = 0;
    dut->noc_in_valid = 0;
    dut->noc_out_busy = 0;
    dut->halt_req = 0;
    for (int i = 0; i < 8; ++i) tick();
    dut->resetn = 1;
    for (int i = 0; i < 4; ++i) tick();

    // A REJECTED BURST MUST NOT WRITE ANYWHERE. Sent BEFORE the real image, so
    // if the bounds check leaks, the image that follows is the thing corrupted
    // and the run fails loudly rather than silently landing somewhere unused.
    if (stress) {
        struct { uint8_t buf; uint64_t off; const char *what; } bad[] = {
            { 3,        0,      "reserved buf_id 3" },
            { 99,       0,      "unknown buf_id" },
            { BUF_IMEM, 100000, "imem offset past the end" },
            { BUF_SPAD, 100000, "spad offset past the end" },
        };
        for (auto &t : bad) {
            Flit h;
            h.header(T_CU_DATA, 0, 2, 2, 1, 1);
            h.pay(255, 8, t.buf);
            h.pay(247, 16, t.off);
            h.pay(231, 8, 1);              // two granules
            send(h);
            for (int g = 0; g < 2; ++g) {
                Flit d;
                d.header(T_CU_DATA, 0, 2, 2, 1, 1);
                for (int j = 0; j < 32; ++j) d.pay(8 * j + 7, 8, 0xA5);
                send(d);
            }
            printf("  rejected   %s\n", t.what);
        }
    }

    send_region(BUF_IMEM, 0,         text_bytes, 1, 1);
    send_region(BUF_SPAD, SPAD_BASE, spad_bytes, 1, 1);
    uint64_t load_cycles = cycles;

    // The kick. op 1 is boot; the unit waits for receive-quiet before it fetches,
    // which is the interlock that stops a program starting before its image.
    Flit k;
    k.header(T_CU_INST, 0x11, 2, 2, 1, 1);
    k.pay(255, 8, 1);
    k.pay(247, 32, entry_pc);
    k.pay(215, 32, 0);
    send(k);

    uint64_t run0 = cycles;
    while (cycles < max_cycles && !sig_seen) tick();

    printf("\n");
    if (!console.empty()) printf("  console    %s", console.c_str());
    printf("  load       %llu cycles\n", (unsigned long long)load_cycles);
    printf("  run        %llu cycles\n", (unsigned long long)(cycles - run0));
    printf("  core       %u cycles, %u retired\n",
           (unsigned)dut->dbg_cycles, (unsigned)dut->dbg_retired);
    if (dut->dbg_cycles)
        printf("  IPC        %.4f\n",
               (double)dut->dbg_retired / (double)dut->dbg_cycles);
    if (sig_seen) printf("  signal     code %u arg %u\n", sig_code, sig_arg);
    else          printf("  TIMEOUT -- no completion\n");

    bool ok = sig_seen && sig_code == 0x00 && sig_arg == 0 &&
              console.find(expect) != std::string::npos;
    printf("========================================\n");
    printf("  %s\n", ok ? "PASS" : "FAIL");
    printf("========================================\n");

    dut->final();
    delete dut;
    delete ctx;
    return ok ? 0 : 1;
}
