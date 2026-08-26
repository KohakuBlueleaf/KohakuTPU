// Two SysCores, two images, both running at once.
//
// THE UNITS ARE INTERLEAVED ON PURPOSE. Loading A fully and then B would pass
// even if the two shared a memory, so the loads alternate granule by granule and
// the kicks go in back to back -- if anything is shared, one image lands on top
// of the other and both programs fail their own checks.
//
// Each unit runs a DIFFERENT program so a crossed console or a crossed
// completion is visible rather than symmetric.

#include "Vrv64_pe_pair.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

static const uint8_t  T_CU_INST = 0x5, T_CU_SIGNAL = 0x6, T_CU_DATA = 0x8;
static const uint8_t  BUF_SPAD = 0, BUF_IMEM = 1;
static const uint64_t SPAD_BASE = 0x00010000ull;

static VerilatedContext *ctx;
static Vrv64_pe_pair *dut;
static uint64_t cycles = 0;

struct Flit {
    uint8_t b[36];
    Flit() { memset(b, 0, sizeof b); }
    void put(int hi_bit, int width, uint64_t v) {
        for (int i = 0; i < width; ++i) {
            int idx = 287 - (hi_bit - i);
            if ((v >> (width - 1 - i)) & 1) b[idx >> 3] |= (uint8_t)(0x80 >> (idx & 7));
        }
    }
    void header(uint8_t ty, uint8_t id, int dx, int dy, int sx, int sy) {
        put(287, 4, dx); put(283, 4, dy); put(279, 4, sx); put(275, 4, sy);
        put(271, 4, ty); put(267, 8, id);
    }
};

static void drive(const Flit &f, uint32_t *w) {
    for (int i = 0; i < 9; ++i) w[i] = 0;
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

// ---- per-unit state --------------------------------------------------------
struct Unit {
    const char *name;
    std::map<uint64_t, uint8_t> image;
    uint64_t entry = 0, text_bytes = 0, spad_bytes = 0;
    std::string console, expect;
    bool sig_seen = false;
    uint8_t sig_code = 0;
    uint32_t sig_arg = 0;
    int pos_x;
};
static Unit A, B;

static void sample() {
    if (dut->a_console_we) A.console.push_back((char)dut->a_console);
    if (dut->b_console_we) B.console.push_back((char)dut->b_console);
    if (dut->a_out_valid && grab(dut->a_out_data, 271, 4) == T_CU_SIGNAL) {
        A.sig_code = (uint8_t)grab(dut->a_out_data, 247, 8);
        A.sig_arg  = (uint32_t)grab(dut->a_out_data, 239, 32);
        A.sig_seen = true;
    }
    if (dut->b_out_valid && grab(dut->b_out_data, 271, 4) == T_CU_SIGNAL) {
        B.sig_code = (uint8_t)grab(dut->b_out_data, 247, 8);
        B.sig_arg  = (uint32_t)grab(dut->b_out_data, 239, 32);
        B.sig_seen = true;
    }
}

static void tick() {
    dut->clk = 0; dut->eval(); sample();
    dut->clk = 1; dut->eval(); ctx->timeInc(1); ++cycles;
}

// Both units are offered a flit in the same cycle and each is retired when its
// own busy clears, so a slow unit never gates the other.
static void send2(const Flit *fa, const Flit *fb) {
    bool wa = fa != nullptr, wb = fb != nullptr;
    if (wa) { drive(*fa, dut->a_in_data); dut->a_in_valid = 1; }
    if (wb) { drive(*fb, dut->b_in_data); dut->b_in_valid = 1; }
    for (int guard = 0; guard < 20000 && (wa || wb); ++guard) {
        dut->clk = 0; dut->eval(); sample();
        bool ta = wa && !dut->a_in_busy;
        bool tb = wb && !dut->b_in_busy;
        dut->clk = 1; dut->eval(); ctx->timeInc(1); ++cycles;
        if (ta) { wa = false; dut->a_in_valid = 0; }
        if (tb) { wb = false; dut->b_in_valid = 0; }
    }
    dut->a_in_valid = 0;
    dut->b_in_valid = 0;
}

static bool load_elf(Unit &u, const char *path) {
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
    u.entry = u64(24);
    uint64_t phoff = u64(32);
    uint16_t phes = u16(54), phnum = u16(56);
    for (int i = 0; i < phnum; ++i) {
        size_t p = phoff + (size_t)i * phes;
        uint32_t ty = (uint32_t)(b[p] | (b[p+1] << 8) | (b[p+2] << 16) | (b[p+3] << 24));
        if (ty != 1) continue;
        uint64_t off = u64(p+8), va = u64(p+16), fsz = u64(p+32), msz = u64(p+40);
        for (uint64_t k = 0; k < fsz; ++k) u.image[va + k] = b[off + k];
        for (uint64_t k = fsz; k < msz; ++k) u.image[va + k] = 0;
    }
    for (auto &kv : u.image) {
        if (kv.first < SPAD_BASE) {
            if (kv.first + 1 > u.text_bytes) u.text_bytes = kv.first + 1;
        } else {
            uint64_t o = kv.first - SPAD_BASE;
            if (o + 1 > u.spad_bytes) u.spad_bytes = o + 1;
        }
    }
    printf("  %s: text %llu, spad %llu, entry %llx\n", u.name,
           (unsigned long long)u.text_bytes, (unsigned long long)u.spad_bytes,
           (unsigned long long)u.entry);
    return true;
}

static uint8_t img(Unit &u, uint64_t a) {
    auto it = u.image.find(a);
    return it == u.image.end() ? 0 : it->second;
}

static Flit gran(Unit &u, uint64_t base, uint64_t g) {
    Flit d;
    d.header(T_CU_DATA, 0, u.pos_x, 2, 1, 1);
    for (int j = 0; j < 32; ++j) d.put(8 * j + 7, 8, img(u, base + g * 32 + j));
    return d;
}

// One region on both units at once, granule by granule.
static void load_both(uint8_t buf, uint64_t base_a, uint64_t na,
                                   uint64_t base_b, uint64_t nb) {
    uint64_t ga = (na + 31) / 32, gb = (nb + 31) / 32;
    if (ga) {
        Flit h; h.header(T_CU_DATA, 0, A.pos_x, 2, 1, 1);
        h.put(255, 8, buf); h.put(247, 16, 0); h.put(231, 8, ga - 1);
        Flit h2; h2.header(T_CU_DATA, 0, B.pos_x, 2, 1, 1);
        h2.put(255, 8, buf); h2.put(247, 16, 0); h2.put(231, 8, gb - 1);
        send2(&h, gb ? &h2 : nullptr);
    }
    uint64_t g = 0;
    while (g < ga || g < gb) {
        Flit da = gran(A, base_a, g), db = gran(B, base_b, g);
        send2(g < ga ? &da : nullptr, g < gb ? &db : nullptr);
        ++g;
    }
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    const char *ea = nullptr, *eb = nullptr;
    uint64_t max_cycles = 20000000;
    A.name = "A"; A.pos_x = 2; A.expect = "pe ok";
    B.name = "B"; B.pos_x = 3; B.expect = "dhrystone ok";
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--elf-a" && i + 1 < argc) ea = argv[++i];
        else if (a == "--elf-b" && i + 1 < argc) eb = argv[++i];
        else if (a == "--expect-a" && i + 1 < argc) A.expect = argv[++i];
        else if (a == "--expect-b" && i + 1 < argc) B.expect = argv[++i];
        else if (a == "--max-cycles" && i + 1 < argc)
            max_cycles = strtoull(argv[++i], nullptr, 0);
    }
    if (!ea || !eb) { printf("  --elf-a and --elf-b are required\n"); return 2; }
    if (!load_elf(A, ea) || !load_elf(B, eb)) return 2;

    dut = new Vrv64_pe_pair(ctx);
    dut->resetn = 0;
    dut->a_in_valid = dut->b_in_valid = 0;
    dut->a_out_busy = dut->b_out_busy = 0;
    for (int i = 0; i < 8; ++i) tick();
    dut->resetn = 1;
    for (int i = 0; i < 4; ++i) tick();

    load_both(BUF_IMEM, 0, A.text_bytes, 0, B.text_bytes);
    load_both(BUF_SPAD, SPAD_BASE, A.spad_bytes, SPAD_BASE, B.spad_bytes);
    uint64_t load_done = cycles;

    Flit ka; ka.header(T_CU_INST, 0x11, A.pos_x, 2, 1, 1);
    ka.put(255, 8, 1); ka.put(247, 32, A.entry); ka.put(215, 32, 0);
    Flit kb; kb.header(T_CU_INST, 0x12, B.pos_x, 2, 1, 1);
    kb.put(255, 8, 1); kb.put(247, 32, B.entry); kb.put(215, 32, 0);
    send2(&ka, &kb);

    uint64_t run0 = cycles;
    while (cycles < max_cycles && !(A.sig_seen && B.sig_seen)) tick();

    printf("\n  load       %llu cycles (both units, interleaved)\n",
           (unsigned long long)load_done);
    printf("  wall       %llu cycles for both to complete\n",
           (unsigned long long)(cycles - run0));
    printf("  A core     %u cycles, %u retired   signal code %u arg %u\n",
           (unsigned)dut->a_cycles, (unsigned)dut->a_retired, A.sig_code, A.sig_arg);
    printf("  B core     %u cycles, %u retired   signal code %u arg %u\n",
           (unsigned)dut->b_cycles, (unsigned)dut->b_retired, B.sig_code, B.sig_arg);

    bool oka = A.sig_seen && A.sig_code == 0 && A.sig_arg == 0 &&
               A.console.find(A.expect) != std::string::npos;
    bool okb = B.sig_seen && B.sig_code == 0 && B.sig_arg == 0 &&
               B.console.find(B.expect) != std::string::npos;
    // A crossed console is the failure this bench exists to catch.
    bool clean = A.console.find(B.expect) == std::string::npos &&
                 B.console.find(A.expect) == std::string::npos;
    printf("  A says     %s", A.console.c_str());
    printf("  B says     %s", B.console.c_str());
    if (!clean) printf("  CROSSED -- a unit printed the other's output\n");

    printf("========================================\n");
    printf("  %s\n", (oka && okb && clean) ? "PASS" : "FAIL");
    printf("========================================\n");

    dut->final();
    delete dut;
    delete ctx;
    return (oka && okb && clean) ? 0 : 1;
}
