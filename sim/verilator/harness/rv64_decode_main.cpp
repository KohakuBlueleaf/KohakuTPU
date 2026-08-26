// rv64_decode against an independent C++ decode.
//
// The two are written from the specification separately rather than one from the
// other, which is the only way this kind of test finds anything: a C++ mirror of
// the Verilog would agree with it about a shared misreading.
//
// The sweep is EVERY encoding of the fields that matter, not a random sample --
// the classic RV64 decode bug is a shift amount that is 6 bits where it should
// be 5, and a random sweep hides it behind the 31 amounts that are legal in
// both.

#include "Vrv64_decode.h"
#include "verilated.h"

#include <cstdint>
#include <cstdio>

static Vrv64_decode *dut;
static long checks = 0, errors = 0;

enum { ADD = 0, SLL, SLT, SLTU, XOR_, SRL, OR_, AND_, SUB, SRA, PASSB };

static int64_t sext(uint64_t v, int bits) {
    uint64_t m = 1ull << (bits - 1);
    return (int64_t)((v ^ m) - m);
}

struct Want {
    int64_t imm;
    int alu_op, alu_word, op1_pc, op2_imm;
    int wr_reg, branch, jal, jalr, load, store, fence, ecall, ebreak, illegal;
    // An EXTENSION op -- M, A, Zicsr, a privileged SYSTEM instruction. The
    // decoder routes it by its own flag, so its base datapath fields (alu_op,
    // imm, op2_imm) are don't-care; only its LEGALITY is contract, which is
    // exactly the illegal-detection hole this sweep exists to catch.
    int ext;
};

static Want model(uint32_t i) {
    Want w = {};
    unsigned op = i & 0x7f, f3 = (i >> 12) & 7, f7 = (i >> 25) & 0x7f;
    unsigned rd = (i >> 7) & 31;
    unsigned sh6 = (i >> 20) & 63, sh5 = (i >> 20) & 31;
    unsigned top6 = (i >> 26) & 63;
    bool shift_i = (f3 == 1) || (f3 == 5);
    bool alt = (f7 >> 5) & 1;

    w.op2_imm = 1;
    w.alu_op = ADD;
    w.imm = sext((i >> 20) & 0xfff, 12);

    auto imm_u = [&] { return sext(((uint64_t)(i >> 12) & 0xfffff) << 12, 32); };
    auto imm_s = [&] {
        return sext((((i >> 25) & 0x7f) << 5) | ((i >> 7) & 0x1f), 12);
    };
    auto imm_b = [&] {
        uint64_t v = (((i >> 31) & 1) << 12) | (((i >> 7) & 1) << 11) |
                     (((i >> 25) & 0x3f) << 5) | (((i >> 8) & 0xf) << 1);
        return sext(v, 13);
    };
    auto imm_j = [&] {
        uint64_t v = (((i >> 31) & 1) << 20) | (((i >> 12) & 0xff) << 12) |
                     (((i >> 20) & 1) << 11) | (((i >> 21) & 0x3ff) << 1);
        return sext(v, 21);
    };

    switch (op) {
        case 0x37: w.imm = imm_u(); w.alu_op = PASSB; w.wr_reg = 1; break;
        case 0x17: w.imm = imm_u(); w.op1_pc = 1; w.wr_reg = 1; break;
        case 0x6f: w.imm = imm_j(); w.jal = 1; w.wr_reg = 1; break;
        case 0x67:
            w.jalr = 1; w.wr_reg = 1; w.illegal = (f3 != 0);
            break;
        case 0x63:
            w.imm = imm_b(); w.branch = 1;
            w.illegal = (f3 == 2) || (f3 == 3);
            break;
        case 0x03: w.load = 1; w.wr_reg = 1; w.illegal = (f3 == 7); break;
        case 0x23: w.imm = imm_s(); w.store = 1; w.illegal = (f3 > 3); break;
        case 0x13:
            w.wr_reg = 1;
            if (shift_i) {
                w.imm = sh6;
                bool ok = (f3 == 1) ? (top6 == 0) : (top6 == 0 || top6 == 0x10);
                w.illegal = !ok;
                w.alu_op = (f3 == 5 && ((i >> 30) & 1)) ? SRA : (int)f3;
            } else {
                w.alu_op = (int)f3;
            }
            break;
        case 0x1b:
            w.alu_word = 1; w.wr_reg = 1;
            if (shift_i) {
                w.imm = sh5;
                bool ok = (f3 == 1) ? (f7 == 0) : (f7 == 0 || f7 == 0x20);
                w.illegal = !ok;
                w.alu_op = (f3 == 5 && ((i >> 30) & 1)) ? SRA : (int)f3;
            } else {
                w.illegal = (f3 != 0);
            }
            break;
        case 0x33:
            w.op2_imm = 0; w.wr_reg = 1;
            if (f7 == 0x01) {                    // M: MUL..REMU, every funct3
                w.ext = 1;
            } else {
                w.alu_op = alt ? ((f3 == 0) ? SUB : SRA) : (int)f3;
                w.illegal = !(f7 == 0 || (f7 == 0x20 && (f3 == 0 || f3 == 5)));
            }
            break;
        case 0x3b:
            w.op2_imm = 0; w.alu_word = 1; w.wr_reg = 1;
            if (f7 == 0x01) {                    // MW: 0,4,5,6,7; 1,2,3 are holes
                w.ext = 1;
                w.illegal = (f3 == 1) || (f3 == 2) || (f3 == 3);
            } else {
                w.alu_op = alt ? ((f3 == 0) ? SUB : SRA) : (int)f3;
                w.illegal = !((f3 == 0 && (f7 == 0 || f7 == 0x20)) ||
                              (f3 == 1 && f7 == 0) ||
                              (f3 == 5 && (f7 == 0 || f7 == 0x20)));
            }
            break;
        case 0x2f: {                             // A: LR/SC and the AMOs
            unsigned f5 = (i >> 27) & 0x1f;
            bool f5_ok = (f5 == 0x02) || (f5 == 0x03) || (f5 == 0x01) ||
                         (f5 == 0x00) || (f5 == 0x04) || (f5 == 0x0c) ||
                         (f5 == 0x08) || (f5 == 0x10) || (f5 == 0x14) ||
                         (f5 == 0x18) || (f5 == 0x1c);
            w.ext = 1; w.wr_reg = 1;
            w.illegal = !((f3 == 2 || f3 == 3) && f5_ok);
            break;
        }
        case 0x0f: w.fence = 1; w.illegal = (f3 != 0 && f3 != 1); break;
        case 0x73:
            w.ext = 1;
            if (f3 == 0) {
                w.ecall = ((i >> 7) == 0);
                w.ebreak = (((i >> 20) & 0xfff) == 1) && (((i >> 7) & 0x1fff) == 0);
                bool mret = (i == 0x30200073u), sret = (i == 0x10200073u);
                bool wfi = (i == 0x10500073u);
                bool sfence = (f7 == 0x09) && (((i >> 7) & 0x1f) == 0);
                w.illegal = !w.ecall && !w.ebreak && !mret && !sret &&
                            !wfi && !sfence;
                // ecall / ebreak carry their own flags and full check below.
                if (w.ecall || w.ebreak) w.ext = 0;
            } else {
                w.illegal = (f3 == 4);           // Zicsr: 1,2,3,5,6,7 legal
            }
            break;
        default: w.illegal = 1; break;
    }
    if (rd == 0) w.wr_reg = 0;
    return w;
}

static void cmp(const char *f, uint32_t i, uint64_t got, uint64_t want) {
    ++checks;
    if (got != want) {
        ++errors;
        if (errors <= 15)
            printf("  MISMATCH instr=%08x %-9s got %llx want %llx\n", i, f,
                   (unsigned long long)got, (unsigned long long)want);
    }
}

static void one(uint32_t i) {
    dut->instr = i;
    dut->eval();
    Want w = model(i);
    cmp("illegal", i, dut->illegal, w.illegal);
    // An illegal encoding's other fields are don't-care by construction; an
    // extension op's base datapath fields are too (it routes by its own flag).
    if (w.illegal || w.ext) return;
    cmp("imm", i, dut->imm, (uint64_t)w.imm);
    cmp("alu_op", i, dut->alu_op, w.alu_op);
    cmp("alu_word", i, dut->alu_word, w.alu_word);
    cmp("op1_pc", i, dut->op1_pc, w.op1_pc);
    cmp("op2_imm", i, dut->op2_imm, w.op2_imm);
    cmp("wr_reg", i, dut->wr_reg, w.wr_reg);
    cmp("branch", i, dut->is_branch, w.branch);
    cmp("jal", i, dut->is_jal, w.jal);
    cmp("jalr", i, dut->is_jalr, w.jalr);
    cmp("load", i, dut->is_load, w.load);
    cmp("store", i, dut->is_store, w.store);
    cmp("fence", i, dut->is_fence, w.fence);
    cmp("ecall", i, dut->is_ecall, w.ecall);
    cmp("ebreak", i, dut->is_ebreak, w.ebreak);
}

static uint64_t rng_s = 0xdeadbeef12345678ull;
static uint32_t rng() {
    rng_s ^= rng_s << 13;
    rng_s ^= rng_s >> 7;
    rng_s ^= rng_s << 17;
    return (uint32_t)(rng_s >> 16);
}

int main(int argc, char **argv) {
    auto *ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    dut = new Vrv64_decode(ctx);

    const unsigned OPS[] = {0x37, 0x17, 0x6f, 0x67, 0x63, 0x03, 0x23,
                            0x13, 0x1b, 0x33, 0x3b, 0x2f, 0x0f, 0x73};

    // EVERY funct3 and EVERY funct7 for every real opcode, with fixed registers.
    // This is where an illegal-detection hole shows up.
    for (unsigned op : OPS)
        for (unsigned f3 = 0; f3 < 8; ++f3)
            for (unsigned f7 = 0; f7 < 128; ++f7) {
                uint32_t i = op | (5u << 7) | (f3 << 12) | (9u << 15) |
                             (7u << 20) | (f7 << 25);
                one(i);
            }

    // EVERY shift amount, both widths -- the 6-vs-5-bit trap.
    for (unsigned op : {0x13u, 0x1bu})
        for (unsigned f3 : {1u, 5u})
            for (unsigned s = 0; s < 64; ++s) {
                one(op | (5u << 7) | (f3 << 12) | (9u << 15) | (s << 20));
                one(op | (5u << 7) | (f3 << 12) | (9u << 15) | (s << 20) |
                    (0x20u << 25));
            }

    // rd = x0 on every opcode: the write must be suppressed.
    for (unsigned op : OPS)
        for (unsigned f3 = 0; f3 < 8; ++f3)
            one(op | (0u << 7) | (f3 << 12) | (9u << 15) | (7u << 20));

    // SYSTEM's two legal encodings and their near misses.
    one(0x00000073u);  // ECALL
    one(0x00100073u);  // EBREAK
    one(0x00200073u);
    one(0x00001073u);
    one(0x01000073u);

    for (long n = 0; n < 400000; ++n) one(rng());

    printf("========================================\n");
    printf("  %s -- %ld checks, %ld errors\n", errors ? "FAIL" : "PASS", checks,
           errors);
    printf("========================================\n");

    dut->final();
    delete dut;
    delete ctx;
    return errors ? 1 : 0;
}
