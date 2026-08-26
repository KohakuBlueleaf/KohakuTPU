// rv64_muldiv against C++, including the three special cases the RISC-V spec
// defines and that no loop produces by itself: divide by zero, the signed
// overflow -2^63 / -1, and the W forms' extension rules.

#include "Vrv64_muldiv.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <vector>

enum { MUL = 0, MULH, MULHSU, MULHU, DIV, DIVU, REM, REMU };
static const char *NAME[] = {"mul", "mulh", "mulhsu", "mulhu",
                             "div", "divu", "rem",    "remu"};

static VerilatedContext *ctx;
static Vrv64_muldiv *dut;
static long checks = 0, errors = 0;

static void tick() {
    dut->clk = 0;
    dut->eval();
    dut->clk = 1;
    dut->eval();
    ctx->timeInc(1);
}

static uint64_t golden(int f3, bool w, uint64_t a, uint64_t b) {
    bool as = (f3 == MUL) || (f3 == MULH) || (f3 == MULHSU) || (f3 == DIV) ||
              (f3 == REM);
    bool bs = (f3 == MUL) || (f3 == MULH) || (f3 == DIV) || (f3 == REM);
    uint64_t A = w ? (as ? (uint64_t)(int64_t)(int32_t)a : (uint32_t)a) : a;
    uint64_t B = w ? (bs ? (uint64_t)(int64_t)(int32_t)b : (uint32_t)b) : b;

    uint64_t r;
    if (f3 < DIV) {
        __int128 p;
        switch (f3) {
            case MUL:    p = (__int128)(int64_t)A * (int64_t)B; break;
            case MULH:   p = (__int128)(int64_t)A * (int64_t)B; break;
            case MULHSU: p = (__int128)(int64_t)A * (unsigned __int128)B; break;
            default:     p = (__int128)((unsigned __int128)A *
                                        (unsigned __int128)B); break;
        }
        r = (f3 == MUL) ? (uint64_t)p : (uint64_t)((unsigned __int128)p >> 64);
    } else {
        bool sgn = (f3 == DIV) || (f3 == REM);
        if (B == 0) {
            r = ((f3 == DIV) || (f3 == DIVU)) ? ~0ull : A;
        } else if (sgn && A == 0x8000000000000000ull && B == ~0ull) {
            r = (f3 == DIV) ? 0x8000000000000000ull : 0ull;
        } else if (sgn) {
            int64_t q = (int64_t)A / (int64_t)B, m = (int64_t)A % (int64_t)B;
            r = (uint64_t)((f3 == DIV) ? q : m);
        } else {
            r = (f3 == DIVU) ? (A / B) : (A % B);
        }
    }
    return w ? (uint64_t)(int64_t)(int32_t)(uint32_t)r : r;
}

static void one(int f3, bool w, uint64_t a, uint64_t b) {
    dut->f3 = f3;
    dut->word = w;
    dut->a = a;
    dut->b = b;
    dut->start = 1;
    tick();
    dut->start = 0;
    int guard = 0;
    while (!dut->done && guard++ < 200) tick();
    ++checks;
    uint64_t want = golden(f3, w, a, b);
    if (guard >= 200) {
        ++errors;
        if (errors <= 10)
            printf("  HANG %-7s%s a=%016llx b=%016llx\n", NAME[f3],
                   w ? ".w" : "  ", (unsigned long long)a,
                   (unsigned long long)b);
        return;
    }
    if ((uint64_t)dut->y != want) {
        ++errors;
        if (errors <= 15)
            printf("  MISMATCH %-7s%s a=%016llx b=%016llx got %016llx want %016llx\n",
                   NAME[f3], w ? ".w" : "  ", (unsigned long long)a,
                   (unsigned long long)b, (unsigned long long)dut->y,
                   (unsigned long long)want);
    }
    tick();
}

static uint64_t rs = 0x9e3779b97f4a7c15ull;
static uint64_t rng() {
    rs ^= rs << 13;
    rs ^= rs >> 7;
    rs ^= rs << 17;
    return rs;
}

int main(int argc, char **argv) {
    ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    dut = new Vrv64_muldiv(ctx);
    dut->resetn = 0;
    dut->start = 0;
    for (int i = 0; i < 4; ++i) tick();
    dut->resetn = 1;

    std::vector<uint64_t> c = {0ull,
                               1ull,
                               2ull,
                               3ull,
                               ~0ull,
                               0xfffffffffffffffeull,
                               0x7fffffffffffffffull,
                               0x8000000000000000ull,
                               0xffffffffull,
                               0x100000000ull,
                               0x7fffffffull,
                               0x80000000ull,
                               0x0123456789abcdefull,
                               0xfedcba9876543210ull};

    for (int f3 = MUL; f3 <= REMU; ++f3)
        for (int w = 0; w < 2; ++w) {
            // MULH/MULHSU/MULHU have no W form; the decoder refuses them.
            if (w && f3 >= MULH && f3 <= MULHU) continue;
            for (uint64_t a : c)
                for (uint64_t b : c) one(f3, w, a, b);
        }

    for (long n = 0; n < 4000; ++n) {
        uint64_t a = rng(), b = rng();
        for (int f3 = MUL; f3 <= REMU; ++f3) {
            one(f3, 0, a, b);
            if (!(f3 >= MULH && f3 <= MULHU)) one(f3, 1, a, b);
        }
    }

    printf("========================================\n");
    printf("  %s -- %ld checks, %ld errors\n", errors ? "FAIL" : "PASS", checks,
           errors);
    printf("========================================\n");

    dut->final();
    delete dut;
    delete ctx;
    return errors ? 1 : 0;
}
