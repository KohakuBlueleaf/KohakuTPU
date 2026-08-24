"""Level-3 and level-4 programs: real software, run through the real memory path.

Each case is a program plus the DRAM and scratchpad it expects to find, and the
answer the golden model gets from running it.  The Verilog bench loads the same
image through a CU_DATA burst into the instruction window, kicks the PE, and
compares the halt word and the resulting memory against these files.

    python tests/pe/tools/rv_sys_gen.py

The DRAM contents are checked with a POSITION-WEIGHTED checksum after the
program has flushed.  A checksum catches the thing a halt word cannot: a
writeback that carried the right bytes to the wrong line, which is what a
mis-tagged eviction looks like.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from rv_asm import assemble, to_hex
from rv_gen import MASK32, SYMS, _sum, zero_regs
from rv_model import DRAM_BASE, Machine

ROOT = pathlib.Path(__file__).resolve().parents[3]

DRAM_WORDS = 4096  # what the bench's AXI RAM covers, in 32-bit words
SPAD_WORDS = 2048


def wrap(body):
    """Registers zeroed, then the program. No pinned bases: these use `li`."""
    return zero_regs() + body


FLUSH = """
    li  t6, CTL_FLUSH
    sw  x0, 0(t6)
"""

# ------------------------------------------------------------------ programs

ARRAY_SUM = wrap(
    """
    li  t0, DRAM
    li  t1, 64
    li  t2, 0
sum_loop:
    lw  t3, 0(t0)
    add t2, t2, t3
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, sum_loop
    li  t4, DRAM+1024
    sw  t2, 0(t4)
"""
    + FLUSH
    + """
    mv  a0, t2
    ecall
"""
)

MEMCPY = wrap(
    """
    li  t0, DRAM
    li  t1, DRAM+2048
    li  t2, 300
cp_loop:
    lb  t3, 0(t0)
    sb  t3, 0(t1)
    addi t0, t0, 1
    addi t1, t1, 1
    addi t2, t2, -1
    bnez t2, cp_loop
"""
    + FLUSH
    + """
    li  a0, 0xC0DE
    ecall
"""
)

# The pointer chase is the case a cache cannot help with: every node is on its
# own line, so every step is a miss, an eviction and a refill.
PTR_CHASE = wrap(
    """
    li  t0, DRAM
    li  t1, 0
    li  t4, 0
chase:
    lw  t2, 4(t0)
    add t1, t1, t2
    addi t4, t4, 1
    lw  t0, 0(t0)
    bnez t0, chase
    li  t5, DRAM+2048
    sw  t1, 0(t5)
    sw  t4, 4(t5)
"""
    + FLUSH
    + """
    mv  a0, t1
    ecall
"""
)

CRC32 = wrap(
    """
    li  t0, DRAM
    li  t1, 128
    li  t2, -1
    li  t5, 0xEDB88320
crc_byte:
    lbu t3, 0(t0)
    xor t2, t2, t3
    li  t4, 8
crc_bit:
    andi s1, t2, 1
    srli t2, t2, 1
    beqz s1, crc_no
    xor t2, t2, t5
crc_no:
    addi t4, t4, -1
    bnez t4, crc_bit
    addi t0, t0, 1
    addi t1, t1, -1
    bnez t1, crc_byte
    not t2, t2
    li  t6, DRAM+3072
    sw  t2, 0(t6)
"""
    + FLUSH
    + """
    mv  a0, t2
    ecall
"""
)

# Arguments arrive two ways at once: the kick's own word through the control
# region, and a granule pushed into the scratchpad before the kick.
SPAD_ARGS = wrap("""
    li  t0, CTL_ARG
    lw  a1, 0(t0)
    li  t1, SPAD
    li  t2, 8
    li  t3, 0
arg_loop:
    lw  t4, 0(t1)
    add t3, t3, t4
    addi t1, t1, 4
    addi t2, t2, -1
    bnez t2, arg_loop
    li  t5, CTL_COREID
    lw  t6, 0(t5)
    add t3, t3, t6
    add a0, t3, a1
    ecall
""")

# Stride the whole cache several times over, so eviction and refill of dirty
# lines is the steady state rather than a corner.
THRASH = wrap(
    """
    li  t0, DRAM
    li  t1, 0
    li  t2, 4
outer:
    li  t3, DRAM
    li  t4, 96
inner:
    lw  t5, 0(t3)
    addi t5, t5, 1
    sw  t5, 0(t3)
    add t1, t1, t5
    addi t3, t3, 32
    addi t4, t4, -1
    bnez t4, inner
    addi t2, t2, -1
    bnez t2, outer
"""
    + FLUSH
    + """
    mv  a0, t1
    ecall
"""
)

# invalidate-all must actually drop lines: read, invalidate, read again. The
# bench rewrites DRAM under the PE between the two reads, so a stale line gives
# a different answer rather than the same one.
INVAL_RECHECK = wrap(
    """
    li  s0, DRAM
    lw  s1, 0(s0)
    li  t0, DRAM+4096
    sw  s1, 0(t0)
    li  t1, 1
    sw  t1, 4(t0)
"""
    + FLUSH
    + """
    li  s3, SPAD
iv_wait:
    lw  t2, 0(s3)
    beqz t2, iv_wait
    li  t3, CTL_INVAL
    sw  x0, 0(t3)
    lw  s2, 0(s0)
    sub a0, s2, s1
    ecall
"""
)


# A command processor: poll a doorbell in the local scratchpad, act, clear it.
# A RING, not one mailbox slot: word 0 is the producer index the pusher advances
# LAST, entries live at byte 64 and are never rewritten, so a consumer that is
# behind reads finished entries only -- no handshake back to the producer.
DOORBELL = wrap("""
    li  s0, SPAD
    li  s1, 0
    li  s2, 0
    sw  x0, 0(s0)
db_poll:
    lw  t0, 0(s0)
    beq t0, s2, db_poll
    slli t1, s2, 3
    add t1, t1, s0
    lw  t2, 64(t1)
    lw  t3, 68(t1)
    addi s2, s2, 1
    li  t4, 2
    beq t2, t4, db_stop
    li  t4, 1
    bne t2, t4, db_poll
    add s1, s1, t3
    j   db_poll
db_stop:
    mv  a0, s1
    ecall
""")

INTERACTIVE = [("inval_recheck", INVAL_RECHECK), ("doorbell", DOORBELL)]


def dram_ramp(n=DRAM_WORDS):
    return [(0x1234_0000 + 7 * i) & MASK32 for i in range(n)]


def dram_chase(nodes=24, stride=32):
    """A linked list, one node per cache line, terminated by a null next."""
    m = [0] * DRAM_WORDS
    for k in range(nodes):
        here = DRAM_BASE + k * stride
        nxt = 0 if k == nodes - 1 else DRAM_BASE + (k + 1) * stride
        m[(here - DRAM_BASE) // 4] = nxt
        m[(here - DRAM_BASE) // 4 + 1] = (0x100 + k * 13) & MASK32
    return m


CASES = [
    ("array_sum", ARRAY_SUM, dram_ramp, None, 0),
    ("memcpy", MEMCPY, dram_ramp, None, 0),
    ("ptr_chase", PTR_CHASE, dram_chase, None, 0),
    ("crc32", CRC32, dram_ramp, None, 0),
    (
        "spad_args",
        SPAD_ARGS,
        None,
        lambda: [(0x2000_0000 + i * 3) & MASK32 for i in range(8)],
        0x0BAD_1234,
    ),
    ("thrash", THRASH, dram_ramp, None, 0),
]


def build(outdir):
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    index = []
    for n, (name, src, dgen, sgen, arg) in enumerate(CASES):
        words, _ = assemble(src, base=0, symbols=SYMS)
        m = Machine(imem_words=4096, spad_words=SPAD_WORDS, arg=arg, coreid=0x11)
        m.imem[: len(words)] = words
        dinit = dgen() if dgen else [0] * DRAM_WORDS
        for i, v in enumerate(dinit):
            if v:
                m.dram[DRAM_BASE + 4 * i] = v & MASK32
        if sgen:
            for i, v in enumerate(sgen()):
                m.spad[i] = v & MASK32
        trace, cause, hword = m.run()

        dfin = [m.dram.get(DRAM_BASE + 4 * i, 0) for i in range(DRAM_WORDS)]
        d = outdir / ("sys%02d" % n)
        d.mkdir(exist_ok=True)
        (d / "prog.hex").write_text(to_hex(words))
        (d / "dram.hex").write_text(to_hex(dinit))
        # The final memory word for word, not only its checksum: a checksum
        # says something moved and nothing about what.
        (d / "dfin.hex").write_text(to_hex(dfin))
        (d / "spad.hex").write_text(to_hex(sgen() if sgen else [0] * 64))
        meta = [
            cause,
            hword,
            len(trace),
            _sum(dfin),
            _sum(m.spad),
            arg,
            len(words),
            len(sgen()) if sgen else 0,
        ]
        (d / "meta.hex").write_text(to_hex(meta))
        (d / "name.txt").write_text(name + "\n")
        index.append((n, name, len(words), len(trace)))
        print(
            "  sys%02d %-12s %5d words %7d retired  a0 %08x  cause %d"
            % (n, name, len(words), len(trace), hword, cause)
        )

    # Programs the model cannot run on its own: they wait on the bench, so only
    # the image is emitted and the bench owns the expected answer.
    for n, (name, src) in enumerate(INTERACTIVE):
        words, _ = assemble(src, base=0, symbols=SYMS)
        d = outdir / ("ix%02d" % n)
        d.mkdir(exist_ok=True)
        (d / "prog.hex").write_text(to_hex(words))
        (d / "name.txt").write_text(name + "\n")
        print("  ix%02d  %-12s %5d words  (bench-driven)" % (n, name, len(words)))

    (outdir / "nsys.hex").write_text("%08x\n" % len(CASES))
    (outdir / "index.txt").write_text("".join("%2d %-12s %5d %8d\n" % r for r in index))
    print("  %d system cases into %s" % (len(CASES), outdir))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "tests" / "pe" / "build" / "sys"))
    a = ap.parse_args()
    build(a.out)


if __name__ == "__main__":
    main()
