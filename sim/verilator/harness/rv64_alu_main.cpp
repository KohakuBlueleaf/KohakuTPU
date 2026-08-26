// rv64_alu against C++ as the golden model.
//
// The ALU is combinational, so this needs no clock at all -- set the inputs,
// eval(), compare. That makes it the cheapest possible differential test, and
// the operand set below is chosen so a lost bit or a wrong extension cannot
// pass: every shift amount, both sign boundaries, INT64_MIN, and the 32-bit
// edges the W forms turn on.

#include "Vrv64_alu.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>
#include <vector>

enum { ADD = 0, SLL, SLT, SLTU, XOR_, SRL, OR_, AND_, SUB, SRA, PASSB };

static const char *NAME[] = {"add", "sll",  "slt", "sltu", "xor", "srl",
                             "or",  "and",  "sub", "sra",  "passb"};

static Vrv64_alu *dut;
static long checks = 0, errors = 0;

static uint64_t golden(int op, bool w, uint64_t a, uint64_t b) {
    uint64_t r;
    int64_t sa = (int64_t)a, sb = (int64_t)b;
    unsigned sh = w ? (b & 31) : (b & 63);
    // The W forms shift a 32-bit datum; SRA must see the 32-bit sign.
    uint64_t sl = w ? (uint32_t)a : a;
    int64_t sr = w ? (int64_t)(int32_t)a : sa;

    switch (op) {
        case ADD:   r = a + b; break;
        case SUB:   r = a - b; break;
        case SLL:   r = sl << sh; break;
        case SRL:   r = sl >> sh; break;
        case SRA:   r = (uint64_t)(sr >> sh); break;
        case SLT:   r = (sa < sb) ? 1 : 0; break;
        case SLTU:  r = (a < b) ? 1 : 0; break;
        case XOR_:  r = a ^ b; break;
        case OR_:   r = a | b; break;
        case AND_:  r = a & b; break;
        case PASSB: r = b; break;
        default:    r = 0; break;
    }
    // Every W result is sign-extended from bit 31.
    return w ? (uint64_t)(int64_t)(int32_t)(uint32_t)r : r;
}

static void one(int op, bool w, uint64_t a, uint64_t b) {
    dut->op = op;
    dut->word = w;
    dut->a = a;
    dut->b = b;
    dut->eval();
    uint64_t want = golden(op, w, a, b);
    ++checks;
    if ((uint64_t)dut->y != want) {
        ++errors;
        if (errors <= 12)
            printf("  MISMATCH %-6s%s a=%016llx b=%016llx got %016llx want %016llx\n",
                   NAME[op], w ? ".w" : "  ", (unsigned long long)a,
                   (unsigned long long)b, (unsigned long long)dut->y,
                   (unsigned long long)want);
    }
}

// xorshift64, so the stream is reproducible and does not depend on the host.
static uint64_t rng_s = 0x243f6a8885a308d3ull;
static uint64_t rng() {
    rng_s ^= rng_s << 13;
    rng_s ^= rng_s >> 7;
    rng_s ^= rng_s << 17;
    return rng_s;
}

int main(int argc, char **argv) {
    auto *ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    dut = new Vrv64_alu(ctx);

    std::vector<uint64_t> corners = {
        0ull,
        1ull,
        2ull,
        63ull,
        64ull,
        31ull,
        32ull,
        0x7fffffffull,
        0x80000000ull,
        0xffffffffull,
        0x100000000ull,
        0x7fffffffffffffffull,
        0x8000000000000000ull,
        0xffffffffffffffffull,
        0xfffffffffffffffeull,
        0x0123456789abcdefull,
        0xfedcba9876543210ull,
        0xaaaaaaaaaaaaaaaaull,
        0x5555555555555555ull,
    };

    // Every corner against every corner, every operation, both widths.
    for (int op = ADD; op <= PASSB; ++op)
        for (int w = 0; w < 2; ++w)
            for (uint64_t a : corners)
                for (uint64_t b : corners) one(op, w, a, b);

    // Every shift amount explicitly, since an off-by-one in the amount mux
    // survives a random sweep for a long time.
    for (int op : {SLL, SRL, SRA})
        for (int w = 0; w < 2; ++w)
            for (unsigned s = 0; s < 64; ++s) {
                one(op, w, 0x0123456789abcdefull, s);
                one(op, w, 0x8000000080000000ull, s);
                one(op, w, 0xffffffffffffffffull, s);
            }

    long n = 200000;
    for (long i = 0; i < n; ++i) {
        uint64_t a = rng(), b = rng();
        for (int op = ADD; op <= PASSB; ++op) {
            one(op, 0, a, b);
            one(op, 1, a, b);
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
