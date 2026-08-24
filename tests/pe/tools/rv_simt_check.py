"""The GPU golden model against independently computed expectations.

    python tests/pe/tools/rv_simt_check.py

Every expectation here is COMPUTED, never typed. A hand-written "want" is how a
correct model gets called wrong: the first draft of the strided gather case
asserted two line requests where eight is right, because a stride of eight words
is exactly one 32-byte line apart. The model was correct and the constant was
not, which is the failure mode this file exists to avoid.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import rv_simt_asm                                               # noqa: F401,E402
from rv_asm import assemble                                     # noqa: E402
from rv_simt_model import GpuMachine, LINE_BYTES, MASK, coalesce  # noqa: E402

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
    m.imem[:len(words)] = words
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
chk([sx(v) for v in w.x[11]] == [abs(v) for v in OPERANDS],
    "x11 is |x10| in every lane")
chk([sx(v) for v in w.x[12]] == [abs(v) + 1 for v in OPERANDS],
    "x12 is |x10|+1 in every lane")
chk(w.stack == [], "the IPDOM stack is empty at the halt")
chk(w.hi_water == 2, "one nesting level costs two entries, not one")

print("--- GENUINELY nested divergence, three levels deep, per-lane exact ---")
# Level k's split sits INSIDE level k-1's then block, so the stack actually
# grows. Four sequential if/elses would never exceed two entries, which is the
# mistake this case was written wrong as the first time.
NEST = 3
body = []
for b in range(NEST):
    body += ["        andi   x1, x10, %d" % (1 << b),
             "        split  x1",
             "        addi   x11, x11, %d" % (1 << b)]
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


chk([sx(v) for v in w.x[11]] == [nested_expect(ln) for ln in range(8)],
    "three nested levels reproduce the lane-by-lane reference")
chk(w.hi_water == 2 * NEST,
    "%d nested levels reach %d entries, two per split (got %d)"
    % (NEST, 2 * NEST, w.hi_water))

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
    w.x[10][ln] = (0 if ln < 3 else -1) & MASK      # lanes 3..7 take the THEN
    w.x[11][ln] = 0x100 + ln
m.run()
chk(w.s[2] == 0x103, "vreadfirst read lane 3, the lowest active (got %#x)"
    % w.s[2])

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
chk(w.s[3] == sum(1 << i for i, v in enumerate(vals) if v),
    "ballot is one bit per non-zero lane")
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
    m = run_tier("        rdctl s1, 0\n        vlw2  x5, s1, x6\n        ecall\n",
                 offs)
    want = lines_for([(BASE + (o << 2)) & MASK for o in offs])
    got = m.fills_issued()
    chk(got == want,
        "uniform base, %-16s -> %d line(s), want %d" % (name, got, want))

print("--- the lane-linear tier is ONE request, by construction ---")
for scale, mn in ((2, "vlinw2"), (1, "vlinh1"), (0, "vlinb0")):
    m, w = build("        rdctl s1, 0\n        %s x5, s1\n        ecall\n" % mn,
                 ctl=[BASE] + [0] * 31)
    m.run()
    span = (8 << scale)
    want = 1 if span <= LINE_BYTES else span // LINE_BYTES
    chk(m.fills_issued() == want,
        "%s spans %d bytes -> %d request(s), got %d"
        % (mn, span, want, m.fills_issued()))

print("--- the coalescer serves its own leader on every pass ---")
for name, offs in PATTERNS.items():
    addrs = [(BASE + (o << 2)) & MASK for o in offs]
    passes = coalesce(addrs)
    chk(sum(len(s) for _, s in passes) == len(addrs),
        "%s: every lane is served exactly once" % name)
    chk(len(passes) <= len(addrs), "%s: at most one pass per lane" % name)

print("=" * 40)
if fails:
    print("  FAIL -- %d checks, %d errors" % (checks, len(fails)))
else:
    print("  PASS -- %d checks, 0 errors" % checks)
print("=" * 40)
sys.exit(1 if fails else 0)
