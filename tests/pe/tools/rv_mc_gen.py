"""Level-4 programs: several PEs on one NoC and one MAG, and what they owe.

Four cases, each a set of PER-CORE images plus the answer every core must halt
with.  The Verilog bench (tests/pe/tb/rv_mc_tb.v) loads one image per PE through
a CU_DATA burst, kicks them all, and waits for every completion.

    python tests/pe/tools/rv_mc_gen.py

    iso   independent programs, disjoint DRAM slices -- isolation, and the
          concurrent writeback load on one memory agent
    pp    A pushes, B replies, N rounds -- round-trip latency IN CYCLES, read
          from CTL_CYCLE by the initiator itself
    agg   every worker sums its slice and pushes value-then-flag to core 0
    ho    the DRAM hand-off of memory-map.md: flush, doorbell, invalidate, read

WHERE THE EXPECTED ANSWER COMES FROM.  `iso` is run through the golden model:
halt word, retired count and the whole final DRAM.  The other three WAIT ON
ANOTHER CORE, which the single-core model cannot run, so their answers are
computed here in closed form instead of going unchecked.

EVERY POLL LOOP HAS A SPIN CAP and halts with 0xDEAD00nn when it trips, so a
deadlock arrives as a named halt word in seconds rather than as a watchdog.

SCRATCHPAD WORD MAP, shared with the bench, which pre-zeroes words 0..31:

     0   ping-pong mailbox            2   hand-off: the reader has cached it
     3   hand-off: the data is in DRAM
     9..11   aggregation values from workers 1..3
    17..19   aggregation flags  from workers 1..3, pushed LAST
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from rv_asm import assemble, to_hex                             # noqa: E402
from rv_model import Machine, DRAM_BASE, SPAD_BASE              # noqa: E402
from rv_gen import SYMS, zero_regs, MASK32, _sum                # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[3]

# 32 KB = the bench's AXI RAM at RAM_DEPTH 1024: one 8 KB slice per core, twice
# the 128-line L1, so the thrash case evicts rather than fits.
DRAM_WORDS = 8192
SLICE_WORDS = 2048
SPAD_WORDS = 2048

# PE i sits on router i's local port and carries that router's coordinate.
PE_XY = [(1, 1), (2, 1), (1, 2), (2, 2)]

# ~5 cycles an iteration, so 20000 is ~100k cycles: far past any NoC round trip
# and far short of the bench's own 400k-cycle wait.
SPIN_CAP = 20000

SUM_N = 512            # words summed by iso core 0 and core 1  (64 lines)
COPY_N = 256           # words copied by iso core 2
# EXACTLY the 4 KB cache: source and destination share every set, so all 512
# accesses miss -- 41,881 cycles for 1,579 instructions, the heaviest case here.
COPY_OFF = 4096
THRASH_LINES = 160     # 5 KB against a 4 KB cache: every pass evicts
THRASH_PASSES = 2
ROUNDS = 16            # ping-pong rounds
AGG_N = 256            # words each core sums for the aggregation
HO_N = 64              # words handed off through DRAM
HO_SEED = 0x0BAD_0000
HO_STEP = 0x0000_0011

FLUSH = """
    li  t6, CTL_FLUSH
    sw  x0, 0(t6)
"""


def slice_base(i):
    return DRAM_BASE + i * SLICE_WORDS * 4


def peer_word(core, w, win=0):
    """The software address of another PE's scratchpad word `w`.

    The address IS the routing (docs/arch/pe/programming.md): region, then the
    destination coordinate, then the window, then the word.
    """
    x, y = PE_XY[core]
    return 0x3000_0000 | (x << 24) | (y << 20) | (win << 19) | (w * 4)


def spad_word(w):
    return SPAD_BASE + 4 * w


def ramp(n=DRAM_WORDS):
    return [(0x1234_0000 + 7 * i) & MASK32 for i in range(n)]


# ------------------------------------------------------------------ programs

def iso_sum(core):
    b = slice_base(core)
    return zero_regs() + f"""
    li  t0, {hex(b)}
    li  t1, {SUM_N}
    li  t2, 0
s0_loop:
    lw  t3, 0(t0)
    add t2, t2, t3
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, s0_loop
    li  t4, {hex(b + 4 * SUM_N)}
    sw  t2, 0(t4)
""" + FLUSH + """
    mv  a0, t2
    ecall
"""


def iso_xor(core):
    b = slice_base(core)
    return zero_regs() + f"""
    li  t0, {hex(b)}
    li  t1, {SUM_N}
    li  t2, 0
s1_loop:
    lw  t3, 0(t0)
    xor t2, t2, t3
    lbu t4, 2(t0)
    add t2, t2, t4
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, s1_loop
    li  t5, {hex(b + 4 * SUM_N)}
    sw  t2, 0(t5)
""" + FLUSH + """
    mv  a0, t2
    ecall
"""


def iso_copy(core):
    b = slice_base(core)
    return zero_regs() + f"""
    li  t0, {hex(b)}
    li  t1, {hex(b + COPY_OFF)}
    li  t2, {COPY_N}
s2_loop:
    lw  t3, 0(t0)
    sw  t3, 0(t1)
    addi t0, t0, 4
    addi t1, t1, 4
    addi t2, t2, -1
    bnez t2, s2_loop
""" + FLUSH + """
    li  a0, 0xC0DE
    ecall
"""


def iso_thrash(core):
    b = slice_base(core)
    return zero_regs() + f"""
    li  t2, {THRASH_PASSES}
    li  t1, 0
s3_outer:
    li  t3, {hex(b)}
    li  t4, {THRASH_LINES}
s3_inner:
    lw  t5, 0(t3)
    addi t5, t5, 1
    sw  t5, 0(t3)
    add t1, t1, t5
    addi t3, t3, 32
    addi t4, t4, -1
    bnez t4, s3_inner
    addi t2, t2, -1
    bnez t2, s3_outer
""" + FLUSH + """
    mv  a0, t1
    ecall
"""


def pp_initiator(partner):
    """Round trip measured by the core itself: CTL_CYCLE before the push and
    after the reply, so the number includes this core's own poll loop and
    nothing of the bench."""
    return zero_regs() + f"""
    li  s0, {hex(spad_word(0))}
    li  s1, {hex(peer_word(partner, 0))}
    li  s2, 0
    li  s3, {ROUNDS}
    li  s4, 0
    li  s5, CTL_CYCLE
pp_round:
    addi s2, s2, 1
    lw  s6, 0(s5)
    sw  s2, 0(s1)
    li  s7, {SPIN_CAP}
pp_poll:
    addi s7, s7, -1
    beqz s7, pp_stuck
    lw  s8, 0(s0)
    bne s8, s2, pp_poll
    lw  s9, 0(s5)
    sub s9, s9, s6
    add s4, s4, s9
    bne s2, s3, pp_round
    mv  a0, s4
    ecall
pp_stuck:
    li  a0, 0xDEAD0001
    ecall
"""


def pp_responder(partner):
    return zero_regs() + f"""
    li  s0, {hex(spad_word(0))}
    li  s1, {hex(peer_word(partner, 0))}
    li  s2, 0
    li  s3, {ROUNDS}
pr_round:
    addi s2, s2, 1
    li  s7, {SPIN_CAP}
pr_poll:
    addi s7, s7, -1
    beqz s7, pr_stuck
    lw  s8, 0(s0)
    bne s8, s2, pr_poll
    sw  s2, 0(s1)
    bne s2, s3, pr_round
    li  a0, {ROUNDS}
    ecall
pr_stuck:
    li  a0, 0xDEAD0002
    ecall
"""


def agg_leader(nworkers):
    b = slice_base(0)
    return zero_regs() + f"""
    li  t0, {hex(b)}
    li  t1, {AGG_N}
    li  t2, 0
ag_sum:
    lw  t3, 0(t0)
    add t2, t2, t3
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, ag_sum
    li  s2, 1
    li  s3, {nworkers + 1}
ag_next:
    slli s4, s2, 2
    li  s5, {hex(spad_word(16))}
    add s5, s5, s4
    li  s6, {SPIN_CAP}
ag_poll:
    addi s6, s6, -1
    beqz s6, ag_stuck
    lw  s7, 0(s5)
    beqz s7, ag_poll
    li  s8, {hex(spad_word(8))}
    add s8, s8, s4
    lw  s9, 0(s8)
    add t2, t2, s9
    addi s2, s2, 1
    bne s2, s3, ag_next
    mv  a0, t2
    ecall
ag_stuck:
    li  a0, 0xDEAD0003
    ecall
"""


def agg_worker(core):
    """Value first, flag last, and that ordering is the whole protocol: one
    sender's pushes reach one destination in program order, so a flag the
    leader can see means the value beside it is already there."""
    b = slice_base(core)
    return zero_regs() + f"""
    li  t0, {hex(b)}
    li  t1, {AGG_N}
    li  t2, 0
aw_sum:
    lw  t3, 0(t0)
    add t2, t2, t3
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, aw_sum
    li  s0, {hex(peer_word(0, 8 + core))}
    sw  t2, 0(s0)
    li  s1, {hex(peer_word(0, 16 + core))}
    li  s2, 1
    sw  s2, 0(s1)
    mv  a0, t2
    ecall
"""


def ho_writer(core, reader):
    b = slice_base(core)
    return zero_regs() + f"""
    li  s0, {hex(spad_word(0))}
    li  s1, {SPIN_CAP}
hw_poll:
    addi s1, s1, -1
    beqz s1, hw_stuck
    lw  s2, 8(s0)
    beqz s2, hw_poll
    li  t0, {hex(b)}
    li  t1, {HO_N}
    li  t2, {hex(HO_SEED)}
hw_store:
    sw  t2, 0(t0)
    addi t2, t2, {HO_STEP}
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, hw_store
""" + FLUSH + f"""
    li  s3, {hex(peer_word(reader, 3))}
    li  s4, 1
    sw  s4, 0(s3)
    li  a0, 0xD09E
    ecall
hw_stuck:
    li  a0, 0xDEAD0004
    ecall
"""


def ho_reader(core, writer):
    b = slice_base(writer)
    return zero_regs() + f"""
    li  t0, {hex(b)}
    li  t1, {HO_N}
    li  t2, 0
hr_pre:
    lw  t3, 0(t0)
    add t2, t2, t3
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, hr_pre
    li  s3, {hex(peer_word(writer, 2))}
    li  s4, 1
    sw  s4, 0(s3)
    li  s0, {hex(spad_word(0))}
    li  s1, {SPIN_CAP}
hr_poll:
    addi s1, s1, -1
    beqz s1, hr_stuck
    lw  s2, 12(s0)
    beqz s2, hr_poll
    li  t4, CTL_INVAL
    sw  x0, 0(t4)
    li  t0, {hex(b)}
    li  t1, {HO_N}
    li  t5, 0
hr_post:
    lw  t3, 0(t0)
    add t5, t5, t3
    addi t0, t0, 4
    addi t1, t1, -1
    bnez t1, hr_post
    mv  a0, t5
    ecall
hr_stuck:
    li  a0, 0xDEAD0005
    ecall
"""


# ------------------------------------------------------------------- cases

def case_iso(npe):
    """Four unrelated programs on disjoint slices.  Modelled, so the halt word,
    the retired count AND the final DRAM are all checked."""
    srcs = [iso_sum(0), iso_xor(1), iso_copy(2), iso_thrash(3)][:npe]
    dinit = ramp()
    dfin = list(dinit)
    halt, cause, instret = [], [], []
    for core, src in enumerate(srcs):
        words, _ = assemble(src, base=0, symbols=SYMS)
        x, y = PE_XY[core]
        m = Machine(imem_words=4096, spad_words=SPAD_WORDS, arg=0,
                    coreid=(y << 4) | x)
        m.imem[:len(words)] = words
        for i, v in enumerate(dinit):
            if v:
                m.dram[DRAM_BASE + 4 * i] = v
        trace, c, hw = m.run()
        # ONLY this core's slice: a model holds the whole preloaded image, so
        # merging one wholesale puts initial values back over another's stores.
        lo = (slice_base(core) - DRAM_BASE) // 4
        for addr, val in m.dram.items():
            idx = (addr - DRAM_BASE) // 4
            if lo <= idx < lo + SLICE_WORDS:
                dfin[idx] = val
        halt.append(hw)
        cause.append(c)
        instret.append(len(trace))
    return dict(srcs=srcs, dinit=dinit, dfin=dfin, halt=halt, cause=cause,
                instret=instret, mask=(1 << npe) - 1, check_dram=1, aux=0)


def case_pp(npe):
    srcs, halt, mask = [], [], 0
    for core in range(npe):
        partner = core ^ 1
        if core % 2 == 0:
            srcs.append(pp_initiator(partner))
            halt.append(0)                       # a measured latency, not a constant
        else:
            srcs.append(pp_responder(partner))
            halt.append(ROUNDS)
            mask |= 1 << core
    return dict(srcs=srcs, dinit=ramp(), dfin=ramp(), halt=halt,
                cause=[1] * npe, instret=[0] * npe, mask=mask,
                check_dram=1, aux=ROUNDS)


def case_agg(npe):
    dinit = ramp()

    def slice_sum(core):
        b = (slice_base(core) - DRAM_BASE) // 4
        return sum(dinit[b:b + AGG_N]) & MASK32

    srcs = [agg_leader(npe - 1)]
    halt = [0]
    for core in range(1, npe):
        srcs.append(agg_worker(core))
        halt.append(slice_sum(core))
    halt[0] = sum(slice_sum(c) for c in range(npe)) & MASK32
    return dict(srcs=srcs, dinit=dinit, dfin=dinit, halt=halt,
                cause=[1] * npe, instret=[0] * npe, mask=(1 << npe) - 1,
                check_dram=1, aux=npe - 1)


def case_ho(npe):
    """Writers are the even cores, readers the odd ones.  At four PEs there are
    two hand-offs at once, which is what puts two flush-alls into one memory
    agent's write slots together."""
    dinit = ramp()
    dfin = list(dinit)
    srcs, halt = [], []
    new = [(HO_SEED + k * HO_STEP) & MASK32 for k in range(HO_N)]
    for core in range(npe):
        if core % 2 == 0:
            srcs.append(ho_writer(core, core + 1))
            halt.append(0xD09E)
            b = (slice_base(core) - DRAM_BASE) // 4
            stale = sum(dinit[b:b + HO_N]) & MASK32
            if stale == sum(new) & MASK32:
                raise SystemExit("the hand-off values sum to the stale ones: "
                                 "a failed invalidate would pass")
            dfin[b:b + HO_N] = new
        else:
            srcs.append(ho_reader(core, core - 1))
            halt.append(sum(new) & MASK32)
    return dict(srcs=srcs, dinit=dinit, dfin=dfin, halt=halt,
                cause=[1] * npe, instret=[0] * npe, mask=(1 << npe) - 1,
                check_dram=1, aux=HO_N)


# `iso` is also generated for ONE core, as the uncontended floor every
# multi-core cycle count is read against. The other three need a peer to talk to.
CASES = [("iso", case_iso), ("pp", case_pp), ("agg", case_agg),
         ("ho", case_ho)]

META_N = 24


def write_case(outdir, npe, name, case):
    d = outdir / ("n%d" % npe) / name
    d.mkdir(parents=True, exist_ok=True)
    nprog = []
    for core, src in enumerate(case["srcs"]):
        words, _ = assemble(src, base=0, symbols=SYMS)
        (d / ("core%d.hex" % core)).write_text(to_hex(words))
        nprog.append(len(words))

    meta = [0] * META_N
    meta[0] = npe
    meta[1] = case["check_dram"]
    meta[2] = _sum(case["dfin"])
    meta[3] = case["mask"]
    for i in range(npe):
        meta[4 + i] = case["halt"][i]
        meta[8 + i] = case["cause"][i]
        meta[12 + i] = case["instret"][i]
        meta[16 + i] = nprog[i]
    meta[20] = case["aux"]
    (d / "meta.hex").write_text(to_hex(meta))
    (d / "dfin.hex").write_text(to_hex(case["dfin"]))
    print("  n%d %-4s %s  halt %s" %
          (npe, name, " ".join("%4dw" % n for n in nprog),
           " ".join("%08x" % h for h in case["halt"][:npe])))


def build(outdir):
    outdir = pathlib.Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "dram.hex").write_text(to_hex(ramp()))
    for npe in (1, 2, 4):
        for name, fn in CASES:
            if (npe > 1) or (name == "iso"):
                write_case(outdir, npe, name, fn(npe))
    print("  multi-core cases into %s" % outdir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "tests" / "pe" / "build" / "mc"))
    a = ap.parse_args()
    build(a.out)


if __name__ == "__main__":
    main()
