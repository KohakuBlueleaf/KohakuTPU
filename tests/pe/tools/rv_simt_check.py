"""The GPU golden model against independently computed expectations.

    python tests/pe/tools/rv_simt_check.py

Every expectation here is COMPUTED, never typed. A hand-written "want" is how a
correct model gets called wrong: the first draft of the strided gather case
asserted two line requests where eight is right, because a stride of eight words
is exactly one 32-byte line apart. The model was correct and the constant was
not, which is the failure mode this file exists to avoid.
"""

import math
import pathlib
import random
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simt_asm  # noqa: F401
from rv_asm import assemble
from rv_simt_model import (
    LINE_BYTES,
    MASK,
    GpuMachine,
    coalesce,
    f16_to_f32,
    f32_to_f16,
)

fails = []
checks = 0


def chk(ok, what):
    global checks
    checks += 1
    if not ok:
        fails.append(what)
        print("  FAIL %s" % what)


def sx(v):
    return v - (1 << 32) if v >> 31 else v


def build(src, lanes=8, waves=1, ctl=None):
    words, _ = assemble(src, base=0)
    m = GpuMachine(lanes=lanes, waves=waves, ctl=ctl)
    m.imem[: len(words)] = words
    return m, m.waves[0]


# ---------------------------------------------------------------- divergence
print("--- 09E 2.2: if/else over eight lanes, per-lane exact ---")
SRC_DIV = """
        slt    x1, x10, x0
        split  x1
        sub    x11, x0, x10
        join
        addi   x11, x10, 0
        join
        addi   x12, x11, 1
        ecall
"""
OPERANDS = [-3, 5, -1, 0, 7, -8, 2, -4]
m, w = build(SRC_DIV)
for ln, v in enumerate(OPERANDS):
    w.x[10][ln] = v & MASK
m.run()
chk(
    [sx(v) for v in w.x[11]] == [abs(v) for v in OPERANDS], "x11 is |x10| in every lane"
)
chk(
    [sx(v) for v in w.x[12]] == [abs(v) + 1 for v in OPERANDS],
    "x12 is |x10|+1 in every lane",
)
chk(w.stack == [], "the IPDOM stack is empty at the halt")
chk(w.hi_water == 2, "one nesting level costs two entries, not one")

print("--- GENUINELY nested divergence, three levels deep, per-lane exact ---")
# Level k's split sits INSIDE level k-1's then block, so the stack actually
# grows. Four sequential if/elses would never exceed two entries, which is the
# mistake this case was written wrong as the first time.
NEST = 3
body = []
for b in range(NEST):
    body += [
        "        andi   x1, x10, %d" % (1 << b),
        "        split  x1",
        "        addi   x11, x11, %d" % (1 << b),
    ]
body += ["        join", "        join"] * NEST
SRC_NEST = "        addi x11, x0, 0\n" + "\n".join(body) + "\n        ecall\n"
m, w = build(SRC_NEST)
for ln in range(8):
    w.x[10][ln] = ln
m.run()


def nested_expect(ln):
    """Level k runs only if every bit below it was also set."""
    total = 0
    for b in range(NEST):
        if not (ln >> b) & 1:
            break
        total += 1 << b
    return total


chk(
    [sx(v) for v in w.x[11]] == [nested_expect(ln) for ln in range(8)],
    "three nested levels reproduce the lane-by-lane reference",
)
chk(
    w.hi_water == 2 * NEST,
    "%d nested levels reach %d entries, two per split (got %d)"
    % (NEST, 2 * NEST, w.hi_water),
)

print("--- IPDOM overflow FAULTS rather than wrapping ---")
deep = ["        addi x1, x0, 1"] + ["        split  x1"] * 6 + ["        ecall"]
m, w = build("\n".join(deep) + "\n")
_, cause, _ = m.run()
chk(cause == 3, "a split past the depth bound halts with cause 3 (got %d)" % cause)

# ------------------------------------------------------------- v -> s paths
print("--- vreadfirst names the LOWEST ACTIVE lane, not lane 0 ---")
SRC_RF = """
        slt    x1, x10, x0
        split  x1
        vreadfirst s2, x11
        join
        join
        ecall
"""
m, w = build(SRC_RF)
for ln in range(8):
    w.x[10][ln] = (0 if ln < 3 else -1) & MASK  # lanes 3..7 take the THEN
    w.x[11][ln] = 0x100 + ln
m.run()
chk(w.s[2] == 0x103, "vreadfirst read lane 3, the lowest active (got %#x)" % w.s[2])

print("--- ballot and redux still reduce ACROSS lanes ---")
SRC_BAL = """
        ballot   s3, x10
        reduxadd s4, x11
        reduxmax s5, x11
        ecall
"""
m, w = build(SRC_BAL)
vals = [3, 0, 7, 0, 1, 9, 0, 4]
for ln, v in enumerate(vals):
    w.x[10][ln] = v
    w.x[11][ln] = v
m.run()
chk(
    w.s[3] == sum(1 << i for i, v in enumerate(vals) if v),
    "ballot is one bit per non-zero lane",
)
chk(w.s[4] == sum(vals), "reduxadd sums every active lane")
chk(w.s[5] == max(vals), "reduxmax takes the maximum")

# -------------------------------------------------------- addressing tiers
print("--- the three addressing tiers, requests computed not typed ---")
BASE = 0x8000_0000


def lines_for(addrs):
    """What the leader/follower rule must produce, computed independently."""
    return len({a & ~(LINE_BYTES - 1) for a in addrs})


def run_tier(src, offsets):
    m, w = build(src, ctl=[BASE] + [0] * 31)
    for ln, o in enumerate(offsets):
        w.x[6][ln] = o & MASK
    m.run()
    return m


PATTERNS = {
    "contiguous": list(range(8)),
    "stride 8 words": [i * 8 for i in range(8)],
    "all one address": [0] * 8,
    "scattered": [0, 16, 32, 48, 64, 80, 96, 112],
    "reverse": list(reversed(range(8))),
    "negative": [-(i + 1) for i in range(8)],
}
for name, offs in PATTERNS.items():
    m = run_tier("        rdctl s1, 0\n        vlw2  x5, s1, x6\n        ecall\n", offs)
    want = lines_for([(BASE + (o << 2)) & MASK for o in offs])
    got = m.fills_issued()
    chk(got == want, "uniform base, %-16s -> %d line(s), want %d" % (name, got, want))

print("--- the lane-linear tier is ONE request, by construction ---")
for scale, mn in ((2, "vlinw2"), (1, "vlinh1"), (0, "vlinb0")):
    m, w = build(
        "        rdctl s1, 0\n        %s x5, s1\n        ecall\n" % mn,
        ctl=[BASE] + [0] * 31,
    )
    m.run()
    span = 8 << scale
    want = 1 if span <= LINE_BYTES else span // LINE_BYTES
    chk(
        m.fills_issued() == want,
        "%s spans %d bytes -> %d request(s), got %d"
        % (mn, span, want, m.fills_issued()),
    )

print("--- the coalescer serves its own leader on every pass ---")
for name, offs in PATTERNS.items():
    addrs = [(BASE + (o << 2)) & MASK for o in offs]
    passes = coalesce(addrs)
    chk(
        sum(len(s) for _, s in passes) == len(addrs),
        "%s: every lane is served exactly once" % name,
    )
    chk(len(passes) <= len(addrs), "%s: at most one pass per lane" % name)


# ------------------------------------------------- VX: compares, binary16
# The reference is Python's own IEEE arithmetic (struct 'f' and 'e'), which
# shares no code with the model's bit-level converters.
def f32(b):
    return struct.unpack("<f", struct.pack("<I", b & MASK))[0]


def bits32(x):
    return struct.unpack("<I", struct.pack("<f", x))[0]


def ref_f16_to_f32(h):
    x = struct.unpack("<e", struct.pack("<H", h))[0]
    return None if math.isnan(x) else bits32(x)


def ref_f32_to_f16(b):
    x = f32(b)
    if math.isnan(x):
        return None
    try:
        return struct.unpack("<H", struct.pack("<e", x))[0]
    except OverflowError:
        return 0x7C00 | (0x8000 if x < 0 else 0)


random.seed(7)
SPECIAL = [
    0x0000_0000,
    0x8000_0000,
    0x3F80_0000,
    0xBF80_0000,
    0x7F80_0000,
    0xFF80_0000,
    0x7FC0_0000,
    0x7F80_0001,
    0x0000_0001,
    0x8040_0000,
    0x477F_E000,
    0x477F_F000,
    0x4780_0000,
    0x3380_0000,
    0x3300_0000,
    0x3300_0001,
    0x387F_C000,
    0x3880_0000,
    0xC77F_EFFF,
]
PAIRS = [(a, b) for a in SPECIAL for b in SPECIAL] + [
    (random.getrandbits(32), random.getrandbits(32)) for _ in range(2000)
]

print("--- VX: vflt/vfle/vfeq against Python's float compare ---")
SRC_VX = """
        vflt   x12, x10, x11
        vfle   x13, x10, x11
        vfeq   x14, x10, x11
        vfmin  x15, x10, x11
        vfmax  x16, x10, x11
        vcvthf x17, x10, x11
        vcvtfh x18, x10, 0
        vcvtfh x19, x10, 1
        ecall
"""
bad = {"cmp": 0, "minmax": 0, "f2h": 0, "h2f": 0}
for i in range(0, len(PAIRS), 8):
    chunk = PAIRS[i : i + 8]
    m, w = build(SRC_VX)
    for ln, (a, b) in enumerate(chunk):
        w.x[10][ln], w.x[11][ln] = a, b
    m.run()
    for ln, (a, b) in enumerate(chunk):
        fa, fb = f32(a), f32(b)
        want = (int(fa < fb), int(fa <= fb), int(fa == fb))
        if (w.x[12][ln], w.x[13][ln], w.x[14][ln]) != want:
            bad["cmp"] += 1
        for got, want_max in ((w.x[15][ln], False), (w.x[16][ln], True)):
            gf = f32(got)
            if math.isnan(fa) and math.isnan(fb):
                ok = got == 0x7FC0_0000
            elif math.isnan(fa) or math.isnan(fb):
                ok = got == (b if math.isnan(fa) else a)
            elif fa == fb:
                ok = gf == fa and (got >> 31) == (
                    ((a & b) >> 31) if want_max else ((a | b) >> 31)
                )
            else:
                ok = gf == (max(fa, fb) if want_max else min(fa, fb))
            bad["minmax"] += not ok
        for half, src in ((0, a), (1, b)):
            r = ref_f32_to_f16(src)
            got = (w.x[17][ln] >> (16 * half)) & 0xFFFF
            ok = (
                (got & 0x7FFF) == 0x7E00 and (got >> 15) == (src >> 31)
                if r is None
                else got == r
            )
            bad["f2h"] += not ok
        for half, reg in ((0, 18), (1, 19)):
            h = (a >> (16 * half)) & 0xFFFF
            r = ref_f16_to_f32(h)
            got = w.x[reg][ln]
            ok = (
                (got & 0x7FC0_0000) == 0x7FC0_0000 and (got >> 31) == (h >> 15)
                if r is None
                else got == r
            )
            bad["h2f"] += not ok
n = len(PAIRS)
chk(
    bad["cmp"] == 0,
    "lt/le/eq agree with Python on %d pairs (%d wrong)" % (n, bad["cmp"]),
)
chk(bad["minmax"] == 0, "min/max agree on %d pairs (%d wrong)" % (n, bad["minmax"]))
chk(bad["f2h"] == 0, "f32->f16 agrees on %d values (%d wrong)" % (2 * n, bad["f2h"]))
chk(bad["h2f"] == 0, "f16->f32 agrees on %d values (%d wrong)" % (2 * n, bad["h2f"]))

print("--- every binary16 pattern converts exactly ---")
wrong = 0
for h in range(1 << 16):
    r = ref_f16_to_f32(h)
    got = f16_to_f32(h)
    if r is None:
        wrong += (got & 0x7FC0_0000) != 0x7FC0_0000
    elif got != r or f32_to_f16(got) != h:
        wrong += 1
chk(wrong == 0, "all 65,536 binary16 values survive f16->f32->f16 (%d wrong)" % wrong)

# ------------------------------------------------------------ LDS atomics
print("--- vamoadd: lanes on one address see each other in lane order ---")
SRC_AMO = """
        rdctl  s1, 10
        vamoadd2 x5, s1, x6
        vamomax2 x7, s1, x8
        ecall
"""
m, w = build(SRC_AMO)
inc = [3, 1, 4, 1, 5, 9, 2, 6]
for ln in range(8):
    w.x[5][ln] = inc[ln]
    w.x[6][ln] = 0 if ln < 4 else ln  # lanes 0..3 share word 0
    w.x[7][ln] = 100 + ln
    w.x[8][ln] = 8 + (ln % 2)
m.run()
prefix = [sum(inc[:ln]) for ln in range(4)]
chk(
    [w.x[5][ln] for ln in range(4)] == prefix,
    "the old values are the lane-order prefix sums",
)
chk(m.lds[0] == sum(inc[:4]), "the shared word holds the sum (got %d)" % m.lds[0])
chk(
    all(m.lds[ln] == inc[ln] for ln in range(4, 8)),
    "private words hold their own value",
)
chk(m.lds[8] == 106 and m.lds[9] == 107, "amomax keeps each word's maximum")

# --------------------------------------------- launches and workgroups
print("--- a launch runs every workgroup of its slice, in the slots that fit ---")
# Each wave writes {wg, wave-in-wg} to line wg*wg_waves + wiw of the output.
SRC_WG = """
        rdctl  s1, 0
        rdctl  s2, 6
        rdctl  s3, 7
        rdctl  s4, 9
        sadd   s5, s0, s0
        sadd   s6, s2, s0
loop:
        sbeqz  s4, done
        sadd   s5, s5, s6
        saddi  s4, s4, -1
        sbnez  s4, loop
done:
        sadd   s5, s5, s3
        sslli  s5, s5, 5
        sadd   s5, s1, s5
        sslli  s7, s2, 8
        sor    s7, s7, s3
        s2v    x5, s7
        vsinw2 x5, s5
        ecall
"""
for wg_count, wg_waves, waves in ((5, 2, 4), (7, 3, 8), (1, 4, 4), (9, 1, 2)):
    words, _ = assemble(SRC_WG, base=0)
    m = GpuMachine(lanes=8, waves=waves)
    m.imem[: len(words)] = words
    m.launch(0, BASE, wg_base=10, wg_count=wg_count, wg_waves=wg_waves)
    _, cause, _ = m.run()
    ok = cause == 1
    for g in range(wg_count):
        for j in range(wg_waves):
            addr = BASE + 32 * ((g + 10) * wg_waves + j)
            want = ((g + 10) << 8) | j
            ok &= all(m.dram.get(addr + 4 * ln) == want for ln in range(8))
    chk(
        ok,
        "%d workgroups of %d waves on %d contexts all ran once"
        % (wg_count, wg_waves, waves),
    )

print("--- the barrier orders a workgroup's LDS traffic, and its absence shows ---")
SRC_BAR = """
        rdctl  s2, 7
        rdctl  s3, 10
        sbeqz  s2, writer_done
        saddi  s4, s0, 40
spin:
        saddi  s4, s4, -1
        sbnez  s4, spin
        saddi  s5, s0, 77
        s2v    x5, s5
        vsinw2 x5, s3
writer_done:
        BARRIER
        vlinw2 x6, s3
        sbnez  s2, out
        rdctl  s1, 0
        vsinw2 x6, s1
out:
        ecall
"""


def bar_run(barrier):
    words, _ = assemble(SRC_BAR.replace("BARRIER", barrier), base=0)
    m = GpuMachine(lanes=8, waves=2)
    m.imem[: len(words)] = words
    m.launch(0, BASE, wg_count=1, wg_waves=2, wg_lds=1)
    m.run()
    return m.dram.get(BASE, 0)


chk(bar_run("bar s0, s0") == 77, "with bar, wave 0 reads what wave 1 wrote")
chk(bar_run("addi x0, x0, 0") != 77, "without it the same program reads the stale word")

# -------------------------------------------------------------- streams
print("--- ldsfill, wait, compute, ldsdrain, wait: a streamed copy ---")
SRC_STR = """
        rdctl  s1, 0
        saddi  s2, s0, 3
        sslli  s2, s2, 16
        ldsfill s1, s2
        saddi  s3, s0, 4
        wait   0, s3
        rdctl  s4, 10
        vlinw2 x5, s4
        addi   x5, x5, 1
        vsinw2 x5, s4
        saddi  s6, s1, 512
        saddi  s7, s0, 1
        sslli  s7, s7, 24
        sor    s2, s2, s7
        ldsdrain s6, s2
        wait   1, s3
        rdctl  s10, 17
        s2v    x6, s10
        saddi  s11, s1, 1024
        vsinw2 x6, s11
        ecall
"""
words, _ = assemble(SRC_STR, base=0)
m = GpuMachine(lanes=8, waves=1)
m.imem[: len(words)] = words
for i in range(32):
    m.dram[BASE + 4 * i] = 1000 + i
m.launch(0, BASE, wg_count=1, wg_waves=1, wg_lds=4)
_, cause, _ = m.run()
want = [1001 + i if i < 8 else 1000 + i for i in range(32)]
got = [m.dram.get(BASE + 512 + 4 * i, 0) for i in range(32)]
chk(
    cause == 1 and got == want,
    "the drained copy is the filled one with row 0 incremented",
)
chk(
    m.dram.get(BASE + 1024) == 4,
    "counter 1 read 4 after the drain (got %r)" % m.dram.get(BASE + 1024),
)

print("--- a wait no stream can satisfy is a deadlock, not a hang ---")
m, w = build("        saddi s1, s0, 1\n        wait 2, s1\n        ecall\n")
try:
    m.run()
    chk(False, "an unsatisfiable wait raises")
except RuntimeError as e:
    chk("deadlock" in str(e), "an unsatisfiable wait raises a deadlock")

print("--- a received COUNT burst wakes a waiting wave ---")
SRC_RX = """
        saddi  s1, s0, 2
        wait   1, s1
        rdctl  s2, 0
        rdctl  s3, 10
        vlinw2 x5, s3
        vsinw2 x5, s2
        ecall
"""
words, _ = assemble(SRC_RX, base=0)
m = GpuMachine(lanes=8, waves=1)
m.imem[: len(words)] = words
m.launch(0, BASE, wg_count=1, wg_waves=1, wg_lds=2)
m.receive(9, 0, [500 + i for i in range(16)])
m.run()
chk(
    [m.dram.get(BASE + 4 * i) for i in range(8)] == [500 + i for i in range(8)],
    "buf_id 9 landed in LDS and counted toward counter 1",
)

print("--- ldssend records the burst and credit records the signal ---")
SRC_SEND = """
        rdctl  s3, 10
        saddi  s5, s0, 9
        s2v    x5, s5
        vsinw2 x5, s3
        saddi  s1, s0, 0x123
        sslli  s1, s1, 16
        saddi  s2, s0, 2
        sslli  s2, s2, 24
        ldssend s1, s2
        saddi  s6, s0, 0x21
        saddi  s7, s0, 64
        credit s6, s7
        ecall
"""
words, _ = assemble(SRC_SEND, base=0)
m = GpuMachine(lanes=8, waves=1)
m.imem[: len(words)] = words
m.launch(0, BASE, wg_count=1, wg_waves=1, wg_lds=1)
m.run()
s = m.sent[0] if m.sent else {}
chk(
    s.get("buf_id") == 0x23
    and (s.get("y"), s.get("x")) == (0, 1)
    and s.get("words") == [9] * 8,
    "the send names (0,1) buf 0x23 and carries LDS row 0 (got %r)" % s,
)
chk(
    m.counters[2] == 1 and m.credits == [(0x21, 64)],
    "counter 2 counted it; one credit posted",
)

print("--- sld reads an argument block word by word ---")
SRC_SLD = """
        rdctl  s1, 0
        sld    s2, s1
        saddi  s3, s1, 4
        sld    s4, s3
        sadd   s5, s2, s4
        saddi  s6, s1, 64
        s2v    x5, s5
        vsinw2 x5, s6
        ecall
"""
m, w = build(SRC_SLD, ctl=[BASE] + [0] * 31)
m.dram[BASE], m.dram[BASE + 4] = 1234, 4321
m.run()
chk(
    m.dram.get(BASE + 64) == 5555,
    "sld summed two argument words (got %r)" % m.dram.get(BASE + 64),
)
m, w = build("        saddi s1, s0, 4\n        sld s2, s1\n        ecall\n")
_, cause, _ = m.run()
chk(cause == 3, "sld outside DRAM faults (cause %d)" % cause)

print("--- one access is all DRAM or all LDS and aligned, or it faults whole ---")
LDS_AT = 0x4000_0000
for name, offs, want_cause in (
    ("DRAM and LDS lanes", [0, 4, 8, (LDS_AT - BASE) & MASK, 16, 20, 24, 28], 3),
    ("a misaligned word", [0, 4, 8, 14, 16, 20, 24, 28], 3),
    ("a scratchpad lane", [0, 4, 8, (0x1000_0000 - BASE) & MASK, 16, 20, 24, 28], 3),
    ("all DRAM, aligned", [0, 4, 8, 12, 16, 20, 24, 28], 1),
):
    m, w = build(
        "        rdctl s1, 0\n        vsw0 x5, s1, x6\n        ecall\n",
        ctl=[BASE] + [0] * 31,
    )
    for ln, o in enumerate(offs):
        w.x[6][ln] = o
        w.x[5][ln] = 0x100 + ln
    _, cause, _ = m.run()
    wrote = any(m.dram.get(BASE + 4 * i, 0) for i in range(8))
    chk(cause == want_cause, "%s: cause %d, want %d" % (name, cause, want_cause))
    chk(wrote == (want_cause == 1), "%s: lanes wrote %s" % (name, wrote))

print("=" * 40)
if fails:
    print("  FAIL -- %d checks, %d errors" % (checks, len(fails)))
else:
    print("  PASS -- %d checks, 0 errors" % checks)
print("=" * 40)
sys.exit(1 if fails else 0)
