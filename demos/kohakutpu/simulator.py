"""RUN THIS. The three simulator levels, over the shipped kernels.

Everything printed is measured here, now, on the same artifact the driver would
dispatch to the card. Nothing is quoted from a plan document.
"""

import numpy as np

from kohakutpu import kernels as K
from kohakutpu import ops as O
from kohakutpu import sim

RULE = "=" * 78
rng = np.random.default_rng(0)


def operands(*shapes, scale=0.3):
    return [np.asarray(rng.normal(0, scale, s), np.float16) for s in shapes]


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def grade(got, want) -> str:
    """Relative error against a float64 reference, and a verdict."""
    err = np.abs(np.asarray(got, np.float64) - want).max() / max(
        np.abs(want).max(), 1e-9
    )
    return f"rel {err:.2e} {'ok' if err < 6e-2 else 'BAD'}"


# ----------------------------------------------------------- level 1: numbers
head("LEVEL 1  NUMBERS -- what the machine computes, and where it loses bits")
x, w = operands((64, 128), (64, 128))
ref = x.astype(np.float64) @ w.astype(np.float64).T

print("A matmul is graded against float64, never eyeballed. ~1.5e-2 is CORRECT")
print("for this machine: a cluster's L1 holds MXFP7, not fp16.\n")
for name, kern, want in (
    ("matmul", O.matmul, ref),
    ("linear_silu", K.linear_silu, ref / (1.0 + np.exp(-ref))),
    ("linear_relu", K.linear_relu, np.maximum(ref, 0.0)),
):
    got = sim.observe(kern, x, w)
    print(f"  {name:14} {grade(got.result, want):22} saturated={got.saturated}")

print("\nAn elementwise op never goes near MXFP7, so it is three orders better:")
got = sim.observe(O.silu, x)
print(
    f"  {'silu':14} {grade(got.result, x.astype(np.float64) / (1 + np.exp(-x.astype(np.float64))))}"
)

print("\nSaturation is COUNTED, not hidden. Push a matmul past the fp16 ceiling:")
big = np.full((64, 128), 200.0, np.float16)
got = sim.observe(O.matmul, big, big)
print(f"  matmul of 200s  saturated={got.saturated:,} elements clamped to 65504")

# ------------------------------------------------------------ level 2: cycles
head("LEVEL 2  CYCLES -- what it costs, before anything runs")
print("Analytic, and a BRACKET: the low end charges no fill, the high end every")
print("one. L1 A is double-buffered, so the machine sits between them.\n")

x, w = operands((256, 256), (256, 256))
print("  tiling sweep on linear_silu (256x256x256):")
for gm, gn in ((4, 4), (8, 8), (8, 16), (16, 16)):
    got = sim.cycles(K.linear_silu, x, w, gm=gm, gn=gn)
    print(
        f"    gm={gm:<3} gn={gn:<3} {got.overlapped:>8,}-{got.cycles:<8,} cy   "
        f"MG {got.share('MG'):>4.0%}  VC {got.share('VC'):>4.0%}"
    )
print("\n  The knob is worth ~5x. This is why `tiling.plan` exists.")

print("\n  Per-stage breakdown of one of them:")
for line in sim.cycles(K.linear_silu, x, w).report().splitlines():
    print(f"    {line}")

# -------------------------------------------------------------- level 3: mesh
head("LEVEL 3  MESH -- what actually crosses the machine")
print("`stages` is the number that matters: a top-level stage IS a MAG round")
print("trip. Every one of them writes the result out and reads it back.\n")

x, w = operands((64, 128), (64, 128))
b = operands((64,), scale=0.5)[0]
print(f"  {'kernel':16} {'stages':>6} {'temps':>5} {'dispatch':>8} {'flits':>7}")
for name, kern, args in (
    ("matmul", O.matmul, (x, w)),
    ("linear_silu", K.linear_silu, (x, w)),
    ("linear_bias", K.linear_bias, (x, w, b)),
    ("linear_add", K.linear_add, (x, w, np.zeros((64, 64), np.float16))),
    ("softmax", O.softmax, (x,)),
    ("rmsnorm", K.rmsnorm, (x, x)),
):
    got = sim.observe(kern, *args)
    print(
        f"  {name:16} {got.stages:>6} {len(got.temps):>5} "
        f"{got.counters['dispatches']:>8} {got.counters['flits']:>7,}"
    )

print("\n  linear_bias vs linear_add is the per-channel point: same arithmetic,")
print("  but a bias declared `(N)` rides the resident tile and allocates nothing,")
print("  where a full-shape operand needs a temp and a second pass.")

print("\n  The instruction mix each unit really retired, for linear_silu:")
got = sim.observe(K.linear_silu, x, w)
print(f"    clusters {got.clusters}")
print(f"    cores    {got.cores}")

# ---------------------------------------------------------- all three at once
head("ALL THREE, one run, one kernel")
print(sim.observe(K.linear_silu, *operands((128, 256), (128, 256))).report())

head("FLASH ATTENTION -- the kernel every level was built to measure")
q, k = operands((128, 64), (128, 64), scale=0.25)
v = operands((64, 128), scale=0.5)[0]
print("Query blocking is a KNOB, and it is the whole trade: unblocked streams")
print("keys against every query at once, `qblock` bounds the arena instead.\n")
print(f"  {'mode':22} {'stages':>6} {'temps':>5} {'cycles':>10}")
for name, knobs in (
    ("unblocked", {"block": 64}),
    ("qblock=64", {"block": 64, "qblock": 64}),
    ("causal", {"block": 64, "causal": True}),
):
    got = sim.observe(K.flash_attention, q, k, v, **knobs)
    print(f"  {name:22} {got.stages:>6} {len(got.temps):>5} {got.timing.cycles:>10,}")
print("\n  Four stages per key block, and TWO of them are forced: `weights` is")
print("  computed on a vector core and multiplied on a cluster, and VC -> cluster")
print("  L1 cannot carry MXFP7. See .plan/HARDWARE-WANTS.md section 1.")

print("\n  READ THE TWO COLUMNS AGAINST EACH OTHER. Unblocked and qblock=64 do")
print("  the same arithmetic and cost the same CYCLES -- but one is 10 stages")
print("  and the other 20. LEVEL 2 DOES NOT PRICE THE MAG ROUND TRIP a stage")
print("  barrier costs, so the win only shows at LEVEL 3. Neither level is")
print("  wrong; this is what having three of them is for.")
