"""RUN THIS. One transformer block written at LEVEL 4, kernel by kernel.

`llm.py` writes the same block at the tensor level, where no tiling is stated.
This one spells every kernel out, so the two can be compared line for line --
and so the cost of each piece is visible separately.
"""

import numpy as np
from kohakuaccel.lang import dims, loop, units
from kohakutpu.kernels import LOG2E, flash_attention
from kohakutpu.lang import kernel

from kohakutpu import api as ktpu
from kohakutpu import lang as L
from kohakutpu import sim

M, K, N = dims("M, K, N")
RULE = "=" * 78
LQ, D, HIDDEN, BLOCK = 128, 64, 128, 64
SCALE = D**-0.5
rng = np.random.default_rng(11)


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def sigmoid(v):
    return L.recip(L.exp2(v * -LOG2E) + 1.0)


@kernel
def matmul(a=L.In(..., M, K), b=L.In(N, K), c=L.Out(..., M, N), *, gm=8, gn=8, nk=2):
    """`a @ b.T`, over any leading rank."""
    with units(a.tiles(gm), b.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(a.chunks32(nk)):
            acc += a[i, k] @ b[j, k]
        c[i, j] <<= acc


@kernel
def rmsnorm(
    x=L.In(..., M, N), w=L.In(..., M, N), y=L.Out(..., M, N), *, eps=1e-5, part=8192
):
    """`x * rsqrt(mean(x^2) + eps) * w`, as ONE band.

    The sum is an E8M15 accumulator and never lands in memory, so the scaling
    is a lowering decision rather than the author's. Written staged, summing 64
    squares at RMS 30 reaches 57600 and fp16 stops at 65504 -- after which
    rsqrt returns zero and the layer outputs zeros.
    """
    with units(x.parts(part)) as e:
        ms = L.row_sum((x[e] / N) * x[e])
        y[e] <<= x[e] * L.rsqrt(ms + eps) * w[e]


@kernel
def residual(a=L.In(...), b=L.In(...), y=L.Out(...), *, part=8192):
    """A skip connection is one pass over both."""
    with units(a.parts(part)) as e:
        y[e] <<= a[e] + b[e]


@kernel
def linear_silu(
    x=L.In(..., M, K), w=L.In(N, K), y=L.Out(..., M, N), *, gm=16, gn=16, nk=2
):
    """`silu(x @ w.T)`, the epilogue ON the accumulator."""
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= acc * sigmoid(acc)


def block(x, g1, wq, wk, wv, wo, g2, w1, w2):
    """One transformer block. Every line is a kernel call."""
    h = rmsnorm(x, g1)
    # `wv @ h.T` is V already transposed, which is what attention reads.
    attn = flash_attention(matmul(h, wq), matmul(h, wk), matmul(wv, h), block=BLOCK)
    x = residual(x, matmul(attn, wo))
    h = rmsnorm(x, g2)
    return residual(x, matmul(linear_silu(h, w1), w2))


def reference(x, g1, wq, wk, wv, wo, g2, w1, w2, eps=1e-5):
    def norm(t, g):
        return t * (1 / np.sqrt((t**2).mean(-1, keepdims=True) + eps)) * g

    h = norm(x, g1)
    q, k, v = h @ wq.T, h @ wk.T, h @ wv.T
    s = q @ k.T * SCALE
    e = np.exp(s - s.max(-1, keepdims=True))
    x = x + ((e / e.sum(-1, keepdims=True)) @ v) @ wo.T
    h = norm(x, g2)
    ff = h @ w1.T
    return x + (ff / (1 + np.exp(-ff))) @ w2.T


def w(*shape, s=0.25):
    return (rng.standard_normal(shape) * s).astype(np.float16)


dev = ktpu.script_device("sim")
X = w(LQ, D, s=1.0)
G1, G2 = w(LQ, D, s=0.1) + 1, w(LQ, D, s=0.1) + 1
WQ, WK, WV, WO = (w(D, D) for _ in range(4))
W1, W2 = w(HIDDEN, D), w(D, HIDDEN)

head("THE BLOCK, KERNEL BY KERNEL")
print("  h    = rmsnorm(x, g1)")
print("  attn = flash_attention(matmul(h, wq), matmul(h, wk), matmul(wv, h))")
print("  x    = residual(x, matmul(attn, wo))")
print("  h    = rmsnorm(x, g2)")
print("  x    = residual(x, matmul(linear_silu(h, w1), w2))")
print(f"\n  {dev}")
print(f"  L={LQ} D={D} hidden={HIDDEN} block={BLOCK}")

head("WHAT EACH KERNEL COSTS, ALONE")
print(f"  {'kernel':<16} {'stages':>6} {'temps':>5} {'cycles':>16} {'flits':>7}")
hb = w(LQ, D, s=1.0)
for name, kern, args in (
    ("rmsnorm", rmsnorm, (X, G1)),
    ("matmul", matmul, (X, WQ)),
    ("linear_silu", linear_silu, (X, W1)),
    ("residual", residual, (X, hb)),
):
    got = sim.observe(kern, *args)
    print(
        f"  {name:<16} {got.stages:>6} {len(got.temps):>5} "
        f"{got.timing.overlapped:>7,}-{got.timing.cycles:<8,} "
        f"{got.counters['flits']:>7,}"
    )
q, k, v = w(BLOCK, D), w(BLOCK, D), w(D, BLOCK)
got = sim.observe(flash_attention, q, k, v, block=BLOCK)
print(
    f"  {'flash_attention':<16} {got.stages:>6} {len(got.temps):>5} "
    f"{got.timing.overlapped:>7,}-{got.timing.cycles:<8,} "
    f"{got.counters['flits']:>7,}"
)
print("\n  Attention is the block. Everything else is one or two stages.")

head("THE WHOLE BLOCK, RUN")
scaled = (WQ * SCALE * LOG2E).astype(np.float16)
on = [ktpu.tensor(a).pin() for a in (G1, scaled, WK, WV, WO, G2, W1, W2)]
before = dict(ktpu.stats())
got = block(ktpu.tensor(X), *on).numpy()
moved = {k_: ktpu.stats()[k_] - before[k_] for k_ in before}

want = reference(
    X.astype(np.float64),
    *[a.astype(np.float64) for a in (G1, WQ, WK, WV, WO, G2, W1, W2)],
)
rel = np.abs(got - want) / np.abs(want).max()
print(f"  vs float64: p50 {np.median(rel):.2e}   max {rel.max():.2e}\n")
for name in ("dispatches", "rounds", "flits", "relayouts", "sent", "fetched"):
    print(f"  {name:<12} {moved[name]:>10,}")

head("LEVEL 4 AGAINST LEVEL 5")
print("`llm.py` writes this same block with no tiling stated at all:\n")
print("    h = x.rmsnorm(g1)")
print("    x = x + flash_attention(h @ wq, h @ wk, wv @ h, block=B) @ wo")
print("\n  Same kernels underneath, same numbers. What level 4 buys is the")
print("  KNOBS -- `gm=16, gn=16` on `linear_silu` above is tuned for this")
print("  shape, and the tensor level would have taken `tiling.plan`'s default.")
print("  What it costs is that the tuning is now yours to keep right.")
