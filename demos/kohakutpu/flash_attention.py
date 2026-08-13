"""RUN THIS. Flash attention: what it costs, what it computes, what blocks it.

Every number is measured here. `kernels/attention.py` is the kernel; this is the
account of what it does and what it cannot yet do.
"""

import numpy as np

from kohakutpu import kernels as K
from kohakutpu import sim

RULE = "=" * 78
LOG2E = 1.4426950408889634
rng = np.random.default_rng(0)


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def operands(lq, lkv, dqk, dv, heads=()):
    """`q, k, v` pre-scaled and transposed the way the kernel takes them.

    `q` arrives multiplied by `log2(e)/sqrt(Dqk)` so every `exp2` takes its
    operand directly, and `v` transposed as `(Dv, Lkv)`, the layout a cluster
    reads.
    """
    at = LOG2E / np.sqrt(dqk)
    q = np.asarray(rng.normal(0, 0.25, (*heads, lq, dqk)) * at, np.float16)
    k = np.asarray(rng.normal(0, 0.25, (*heads, lkv, dqk)), np.float16)
    v = np.asarray(rng.normal(0, 0.5, (*heads, dv, lkv)), np.float16)
    return q, k, v


def reference(q, k, v, causal=False):
    """Attention in float64, on operands already scaled and transposed."""
    s = np.float64(q) @ np.float64(k).T
    if causal:
        n = s.shape[0]
        s = np.where(np.tril(np.ones((n, n), bool)), s, -np.inf)
    s = s - s.max(-1, keepdims=True)
    p = np.exp2(s)
    return (p / p.sum(-1, keepdims=True)) @ np.float64(v).T


def rel(got, want) -> float:
    return float(
        np.abs(np.asarray(got, np.float64) - want).max() / max(np.abs(want).max(), 1e-9)
    )


# --------------------------------------------------------- what a stage costs
head("STAGES -- a top-level stage IS a MAG round trip")
print("The kernel streams KEY blocks. Blocking the QUERY axis as well doubles")
print("everything and buys only a smaller arena, so it is a knob, not the")
print("default. The old DSL, which never blocked queries, cost 10 bands here.\n")

q, k, v = operands(128, 128, 64, 64)
print(f"  {'mode':26} {'stages':>6} {'temps':>5} {'rel err':>9}")
for name, knobs in (
    ("unblocked (default)", {"block": 64}),
    ("qblock=64", {"block": 64, "qblock": 64}),
    ("causal", {"block": 64, "causal": True}),
):
    got = sim.observe(K.flash_attention, q, k, v, **knobs)
    want = reference(q, k, v, causal=knobs.get("causal", False))
    print(
        f"  {name:26} {got.stages:>6} {len(got.temps):>5} "
        f"{rel(got.result, want):>9.2e}"
    )

print("\n  Four stages per key block. TWO of the four are forced by hardware:")
print("  `weights` is computed on a vector core and multiplied on a cluster,")
print("  and VC -> cluster L1 cannot carry MXFP7 (HARDWARE-WANTS section 1).")
print("  With that path open a key block would be two stages, not four.")

# ------------------------------------------------------------- heads are free
head("MHA and GQA -- both free, for different reasons")
print("Heads ride the `...` BATCH axis, so multi-head needs no head loop and no")
print("extra stage. GQA stacks a KV head's query heads on the ROW axis, which")
print("the key-block loop already covers -- also no extra stage.\n")

q4, k4, v4 = operands(128, 128, 64, 64, heads=(4,))
got = sim.observe(K.flash_attention, q4, k4, v4, block=64)
want = np.stack([reference(q4[i], k4[i], v4[i]) for i in range(4)])
print(
    f"  4 heads, (H, L, D)      stages={got.stages:>3}  rel {rel(got.result, want):.2e}"
)

qg = np.asarray(rng.normal(0, 0.25, (4, 2 * 128, 64)) * (LOG2E / 8), np.float16)
got = sim.observe(K.flash_attention, qg, k4, v4, block=64, groups=2)
want = np.stack([reference(qg[i], k4[i], v4[i]) for i in range(4)])
print(
    f"  GQA, 2 q-heads per kv   stages={got.stages:>3}  rel {rel(got.result, want):.2e}"
)
print("\n  Same 10 stages as one head. The head axis costs nothing at all.")

# ------------------------------------------------------------ how it scales
head("SCALING -- stages grow with the KEY axis, not the query axis")
print(f"  {'Lq x Lkv':16} {'stages':>6} {'cycles':>12} {'temps':>5}")
for lq, lkv in ((128, 128), (128, 256), (256, 128), (256, 256)):
    q2, k2, v2 = operands(lq, lkv, 64, 64)
    got = sim.observe(K.flash_attention, q2, k2, v2, block=64)
    print(
        f"  {f'{lq} x {lkv}':16} {got.stages:>6} {got.timing.cycles:>12,} "
        f"{len(got.temps):>5}"
    )
print("\n  Doubling Lkv doubles the stages; doubling Lq does not move them.")
print("  Unblocked, the query axis is one pass however long it is -- the temps")
print("  simply get taller, which is exactly the arena `qblock` would bound.")

# --------------------------------------------------------------- the refusals
head("WHAT IT REFUSES, and at which level")
print("A refusal here names its level. None of these is guessed around.\n")
dev = sim.device()
held = [dev.tensor(a) for a in (q, k, v)]
for label, knobs in (
    ("causal with qblock != block", {"block": 64, "qblock": 128, "causal": True}),
    ("causal + GQA", {"block": 64, "causal": True, "groups": 2}),
    ("causal + padded keys", {"block": 64, "causal": True, "keys": 96}),
):
    try:
        K.flash_attention.plan(*held, **knobs)
        print(f"  {label:30} accepted")
    except ValueError as why:
        print(f"  {label:30} {str(why).split(';')[0][:72]}")

print("\n  The GQA one is a DSL limit and worth naming precisely: causal needs")
print("  key blocks 0..(i mod blocks_per_group) for query block i, and `i` is a")
print("  runtime loop variable. There is no modulo on a symbolic index here, so")
print("  the kernel refuses rather than computing the wrong triangle. Call it")
print("  once per group instead.")

head("SUMMARY")
print("  40 -> 10 stages, matching the old DSL's 10 bands at this shape.")
print("  Three things did it, and all three are general:")
print("    1. do not block the query axis unless the arena needs it")
print("    2. keep a correction a VALUE, not a temp -- a re-read is a ninth")
print("       descriptor and the band is refused at eight")
print("    3. heads ride the batch axis, so MHA and GQA cost nothing")
