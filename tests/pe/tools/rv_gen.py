"""Build the co-simulation cases: programs, golden traces, expected state.

Each case becomes tests/pe/build/caseNN/{prog.hex,trace.hex,meta.hex}.  The
Verilog bench walks them in one simulation, so adding a case is one entry here
and nothing in the RTL or the bench.

    python tests/pe/rv_gen.py

WHY EVERY PROGRAM ZEROES ITS REGISTERS FIRST.  The register file is a RAM with
no reset, so it survives between cases while the model starts from zero.  The
prologue makes the two agree without the bench having to reach into the array,
and it costs 31 instructions of a case that runs hundreds.

Random streams are SEEDED and the seeds are listed, so a failure is
reproducible by name rather than by luck.
"""

import argparse
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from rv_asm import assemble, to_hex
from rv_model import CTL_BASE, DRAM_BASE, SPAD_BASE, Machine, trace_hex

ROOT = pathlib.Path(__file__).resolve().parents[3]
SYMS = {
    "SPAD": SPAD_BASE,
    "DRAM": DRAM_BASE,
    "CTL": CTL_BASE,
    "CTL_STATUS": CTL_BASE + 0x00,
    "CTL_FLUSH": CTL_BASE + 0x04,
    "CTL_INVAL": CTL_BASE + 0x08,
    "CTL_CAUSE": CTL_BASE + 0x0C,
    "CTL_COREID": CTL_BASE + 0x10,
    "CTL_ARG": CTL_BASE + 0x14,
    "CTL_CYCLE": CTL_BASE + 0x18,
    "CTL_INSTRET": CTL_BASE + 0x1C,
}

# x31 and x30 are pinned by the prologue and never written again, so a random
# stream can index memory without ever computing an address that is not mapped.
SP_REG, DR_REG = "x31", "x30"

PROLOGUE = """
    li x31, SPAD
    li x30, DRAM
"""


def zero_regs(skip=()):
    out = []
    for i in range(1, 32):
        if ("x%d" % i) in skip:
            continue
        out.append("    addi x%d, x0, 0" % i)
    return "\n".join(out) + "\n"


def prog(body, a0="0"):
    """A complete case: zero the registers, pin the bases, run, halt with a0."""
    return zero_regs() + PROLOGUE + body + "\n    li a0, %s\n    ecall\n" % a0


# ---------------------------------------------------------------- directed


def case_alu_imm():
    vals = [0, 1, -1, 5, -5, 0x7FF, -0x800, 0x55, -0x556]
    b = ["    li x5, 0x12345678", "    li x6, 0xFFFF0000", "    li x7, 0x0000FFFF"]
    for v in vals:
        for op in ("addi", "slti", "sltiu", "xori", "ori", "andi"):
            for src in ("x5", "x6", "x7", "x0"):
                b.append("    %s x8, %s, %d" % (op, src, v))
                b.append("    add x9, x9, x8")
    return prog("\n".join(b), a0="0")


def case_shifts():
    b = ["    li x5, 0x80000001", "    li x6, 0x7FFFFFFF"]
    for sh in range(32):
        for op in ("slli", "srli", "srai"):
            b.append("    %s x8, x5, %d" % (op, sh))
            b.append("    add x9, x9, x8")
            b.append("    %s x8, x6, %d" % (op, sh))
            b.append("    add x9, x9, x8")
    # register shifts, including amounts above 31 which must use only [4:0]
    b.append("    li x10, 33")
    b.append("    li x11, 0xFFFFFFE1")
    for op in ("sll", "srl", "sra"):
        b.append("    %s x8, x5, x10" % op)
        b.append("    add x9, x9, x8")
        b.append("    %s x8, x5, x11" % op)
        b.append("    add x9, x9, x8")
    return prog("\n".join(b))


def case_alu_reg():
    vals = [0, 1, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF, 0x12345678, 0xDEADBEEF]
    b = []
    for i, v in enumerate(vals):
        b.append("    li x%d, 0x%08X" % (5 + i, v))
    for op in ("add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and"):
        for i in range(len(vals)):
            for j in range(len(vals)):
                b.append("    %s x20, x%d, x%d" % (op, 5 + i, 5 + j))
                b.append("    add x21, x21, x20")
    return prog("\n".join(b))


def case_lui_auipc():
    b = []
    for v in (0, 1, 0xFFFFF, 0x80000, 0x12345):
        b.append("    lui x5, 0x%X" % v)
        b.append("    auipc x6, 0x%X" % v)
        b.append("    add x7, x7, x5")
        b.append("    add x7, x7, x6")
    return prog("\n".join(b))


def case_branches():
    vals = [0, 1, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF]
    b = []
    n = 0
    for i, va in enumerate(vals):
        for j, vb in enumerate(vals):
            for op in ("beq", "bne", "blt", "bge", "bltu", "bgeu"):
                b.append("    li x5, 0x%08X" % va)
                b.append("    li x6, 0x%08X" % vb)
                b.append("    %s x5, x6, bt%d" % (op, n))
                b.append("    addi x7, x7, 1")
                b.append("bt%d:" % n)
                b.append("    addi x8, x8, 1")
                n += 1
    # a backward branch, so the predictor sees a loop and not only forward exits
    b.append("    li x9, 20")
    b.append("bloop:")
    b.append("    addi x9, x9, -1")
    b.append("    addi x10, x10, 3")
    b.append("    bnez x9, bloop")
    return prog("\n".join(b))


def case_jumps():
    b = """
    jal x5, j1
    addi x20, x20, 100
j1:
    jal x6, j2
    addi x20, x20, 200
j2:
    la x7, j3
    jalr x8, 0(x7)
    addi x20, x20, 400
j3:
    la x7, j4
    addi x7, x7, 1
    jalr x9, 0(x7)
    addi x20, x20, 800
j4:
    add x21, x5, x6
    add x21, x21, x8
    add x21, x21, x9
    jal x0, j5
    addi x20, x20, 1600
j5:
    addi x22, x22, 7
"""
    return prog(b)


def case_loads_stores():
    b = []
    for base in (SP_REG, DR_REG):
        for off in range(0, 32, 4):
            b.append("    li x5, 0x%08X" % (0x11223344 + off))
            b.append("    sw x5, %d(%s)" % (off, base))
        for off in range(32):
            b.append("    lb  x6, %d(%s)" % (off, base))
            b.append("    add x20, x20, x6")
            b.append("    lbu x6, %d(%s)" % (off, base))
            b.append("    add x20, x20, x6")
        for off in range(0, 32, 2):
            b.append("    lh  x6, %d(%s)" % (off, base))
            b.append("    add x20, x20, x6")
            b.append("    lhu x6, %d(%s)" % (off, base))
            b.append("    add x20, x20, x6")
        for off in range(32):
            b.append("    li x7, %d" % (0xA0 + off))
            b.append("    sb x7, %d(%s)" % (off, base))
            b.append("    lbu x8, %d(%s)" % (off, base))
            b.append("    add x21, x21, x8")
        for off in range(0, 32, 2):
            b.append("    li x7, %d" % (0x1000 + off))
            b.append("    sh x7, %d(%s)" % (off, base))
            b.append("    lhu x8, %d(%s)" % (off, base))
            b.append("    add x21, x21, x8")
        for off in range(0, 32, 4):
            b.append("    lw x9, %d(%s)" % (off, base))
            b.append("    add x22, x22, x9")
    return prog("\n".join(b))


def case_hazards():
    """Every producer-to-consumer spacing from 1 to 6, on both source operands."""
    b = ["    li x5, 7", "    li x6, 11"]
    for dist in range(1, 7):
        for which in (1, 2):
            b.append("    add x10, x5, x6")
            for _ in range(dist - 1):
                b.append("    addi x11, x11, 1")
            if which == 1:
                b.append("    add x12, x10, x6")
            else:
                b.append("    add x12, x6, x10")
            b.append("    add x13, x13, x12")
    # the same spacings with a branch as the consumer
    for dist in range(1, 5):
        b.append("    add x14, x5, x6")
        for _ in range(dist - 1):
            b.append("    addi x11, x11, 1")
        b.append("    beq x14, x14, hz%d" % dist)
        b.append("    addi x15, x15, 99")
        b.append("hz%d:" % dist)
        b.append("    addi x16, x16, 1")
    return prog("\n".join(b))


def case_load_use():
    b = [
        "    li x5, 0x55667788",
        "    sw x5, 0(%s)" % SP_REG,
        "    li x5, 0x99AABBCC",
        "    sw x5, 4(%s)" % SP_REG,
    ]
    for dist in range(1, 6):
        for which in (1, 2):
            b.append("    lw x10, 0(%s)" % SP_REG)
            for _ in range(dist - 1):
                b.append("    addi x11, x11, 1")
            if which == 1:
                b.append("    add x12, x10, x5")
            else:
                b.append("    add x12, x5, x10")
            b.append("    add x13, x13, x12")
    # a load feeding an address calculation, and a load feeding a store's data
    for dist in range(1, 4):
        b.append("    lw x14, 4(%s)" % SP_REG)
        for _ in range(dist - 1):
            b.append("    addi x11, x11, 1")
        b.append("    andi x15, x14, 12")
        b.append("    add x15, x15, %s" % SP_REG)
        b.append("    lw x16, 0(x15)")
        b.append("    add x17, x17, x16")
        b.append("    lw x18, 0(%s)" % SP_REG)
        b.append("    sw x18, 16(%s)" % SP_REG)
        b.append("    lw x19, 16(%s)" % SP_REG)
        b.append("    add x17, x17, x19")
    return prog("\n".join(b))


def case_x0():
    b = """
    li x5, 12345
    addi x0, x5, 1
    add  x0, x5, x5
    lw   x0, 0(x31)
    add  x6, x0, x5
    sw   x0, 0(x31)
    lw   x7, 0(x31)
    beq  x0, x0, k1
    addi x8, x8, 77
k1:
    jal  x0, k2
    addi x8, x8, 88
k2:
    add  x9, x6, x7
"""
    return prog(b)


def case_loop():
    """A hot inner loop: the case the branch predictor exists for."""
    b = """
    li x5, 64
    li x6, 0
    add x7, x0, x31
sum:
    sw x6, 0(x7)
    addi x7, x7, 4
    addi x6, x6, 1
    bne x6, x5, sum
    li x8, 0
    add x7, x0, x31
    li x9, 0
acc:
    lw x10, 0(x7)
    add x9, x9, x10
    addi x7, x7, 4
    addi x8, x8, 1
    bne x8, x5, acc
"""
    return prog(b, a0="0")


def case_fence_nop():
    b = """
    li x5, 3
    fence
    addi x5, x5, 4
    fence
    add x6, x5, x5
"""
    return prog(b)


def case_peer_push():
    """Stores into a peer window: the address decode, not the protocol."""
    b = []
    b.append("    li x5, 0x30112000")  # core (1,1), scratchpad window
    for i in range(8):
        b.append("    li x6, 0x%08X" % (0xC0DE0000 + i))
        b.append("    sw x6, %d(x5)" % (i * 4))
    b.append("    li x7, 0xAA")
    b.append("    sb x7, 33(x5)")
    b.append("    li x7, 0xBBCC")
    b.append("    sh x7, 34(x5)")
    return prog("\n".join(b))


DIRECTED = [
    ("alu_imm", case_alu_imm),
    ("alu_reg", case_alu_reg),
    ("shifts", case_shifts),
    ("lui_auipc", case_lui_auipc),
    ("branches", case_branches),
    ("jumps", case_jumps),
    ("loads_stores", case_loads_stores),
    ("hazards", case_hazards),
    ("load_use", case_load_use),
    ("x0", case_x0),
    ("loop", case_loop),
    ("fence_nop", case_fence_nop),
    ("peer_push", case_peer_push),
]


# ------------------------------------------------------------------ random

RAND_ALU_R = ["add", "sub", "sll", "slt", "sltu", "xor", "srl", "sra", "or", "and"]
RAND_ALU_I = ["addi", "slti", "sltiu", "xori", "ori", "andi"]
RAND_SH_I = ["slli", "srli", "srai"]
RAND_BR = ["beq", "bne", "blt", "bge", "bltu", "bgeu"]


def case_random(seed, n=400):
    """A seeded stream over the whole ISA, constrained only where it must be.

    Two constraints, and both are about keeping the program well defined rather
    than about avoiding hard cases: memory addresses are built from a pinned
    base so nothing lands outside a mapped window, and branches only ever go
    forward so the stream cannot loop.
    """
    rng = random.Random(seed)
    body = []
    # x1..x29 are free; x30 and x31 are the pinned bases.
    free = ["x%d" % i for i in range(1, 30)]
    pending = {}  # label -> emitted?
    for i in range(n):
        for lbl in [k for k, v in pending.items() if v == i]:
            body.append("%s:" % lbl)
            del pending[lbl]
        k = rng.random()
        rd = rng.choice(free + ["x0"])
        a = rng.choice(free + ["x0"])
        b = rng.choice(free + ["x0"])
        if k < 0.30:
            body.append("    %s %s, %s, %s" % (rng.choice(RAND_ALU_R), rd, a, b))
        elif k < 0.52:
            body.append(
                "    %s %s, %s, %d"
                % (rng.choice(RAND_ALU_I), rd, a, rng.randint(-2048, 2047))
            )
        elif k < 0.60:
            body.append(
                "    %s %s, %s, %d" % (rng.choice(RAND_SH_I), rd, a, rng.randint(0, 31))
            )
        elif k < 0.64:
            body.append("    lui %s, 0x%X" % (rd, rng.randint(0, 0xFFFFF)))
        elif k < 0.66:
            body.append("    auipc %s, 0x%X" % (rd, rng.randint(0, 0xFFFFF)))
        elif k < 0.78:
            base = rng.choice([SP_REG, DR_REG])
            sz = rng.choice([("sw", 4), ("sh", 2), ("sb", 1)])
            off = rng.randrange(0, 256, sz[1])
            body.append("    %s %s, %d(%s)" % (sz[0], a, off, base))
        elif k < 0.90:
            base = rng.choice([SP_REG, DR_REG])
            ld = rng.choice([("lw", 4), ("lh", 2), ("lhu", 2), ("lb", 1), ("lbu", 1)])
            off = rng.randrange(0, 256, ld[1])
            body.append("    %s %s, %d(%s)" % (ld[0], rd, off, base))
        else:
            skip = rng.randint(1, 6)
            lbl = "r%d_%d" % (seed, i)
            if i + skip < n:
                pending[lbl] = i + skip
                body.append("    %s %s, %s, %s" % (rng.choice(RAND_BR), a, b, lbl))
            else:
                body.append("    addi %s, %s, 1" % (rd, rd))
    for lbl in pending:
        body.append("%s:" % lbl)
    return prog("\n".join(body))


# -------------------------------------------------------------------- build

MASK32 = 0xFFFFFFFF
DRAM_WORDS = 1024  # the window the bench's flat DRAM model covers


def _sum(words):
    """Position-weighted, so a word landing at the wrong index still shows."""
    return sum((w & MASK32) * (i + 1) for i, w in enumerate(words)) & MASK32


def _peer_sum(log):
    t = 0
    for k, (addr, val, be) in enumerate(log):
        t += (addr * 3 + val * 5 + be * 7) * (k + 1)
    return t & MASK32


def build(outdir, imem_words, spad_words, seeds):
    cases = list(DIRECTED) + [
        ("random%d" % s, (lambda s=s: case_random(s))) for s in seeds
    ]
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    index = []
    for n, (name, fn) in enumerate(cases):
        src = fn()
        words, _ = assemble(src, base=0, symbols=SYMS)
        if len(words) > imem_words:
            raise SystemExit(
                "case %s is %d words, window is %d" % (name, len(words), imem_words)
            )
        m = Machine(imem_words=imem_words, spad_words=spad_words)
        m.imem[: len(words)] = words
        trace, cause, hword = m.run()

        d = outdir / ("case%02d" % n)
        d.mkdir(exist_ok=True)
        (d / "prog.hex").write_text(to_hex(words))
        (d / "trace.hex").write_text(trace_hex(trace))
        dram = [m.dram.get(DRAM_BASE + 4 * i, 0) for i in range(DRAM_WORDS)]
        meta = (
            [len(trace), cause, hword]
            + [v & MASK32 for v in m.x]
            + [_sum(m.spad), _sum(dram), len(m.peer_log), _peer_sum(m.peer_log)]
        )
        (d / "meta.hex").write_text(to_hex(meta))
        (d / "name.txt").write_text(name + "\n")
        index.append((n, name, len(words), len(trace)))
        print(
            "  case%02d %-14s %5d words %6d retired  cause %d"
            % (n, name, len(words), len(trace), cause)
        )

    (outdir / "ncase.hex").write_text("%08x\n" % len(cases))
    (outdir / "index.txt").write_text("".join("%2d %-14s %5d %7d\n" % r for r in index))
    print("  %d cases into %s" % (len(cases), outdir))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "tests" / "pe" / "build"))
    ap.add_argument("--imem-words", type=int, default=2048)
    ap.add_argument("--spad-words", type=int, default=2048)
    ap.add_argument("--seeds", default="1,2,3,4,5,6,7,8")
    a = ap.parse_args()
    build(a.out, a.imem_words, a.spad_words, [int(s) for s in a.seeds.split(",") if s])


if __name__ == "__main__":
    main()
