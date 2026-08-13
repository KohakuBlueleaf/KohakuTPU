"""RUN THIS. A transformer stack at the TENSOR level, and what it really costs.

No tiling is stated anywhere below: every line of the block is a shipped kernel,
so the fluent form cannot disagree with the DSL. `transformer_block.py` spells
the same block out one level down.

The question answered here is not "does it run" but "what would an LLM cost".
"""

import numpy as np
from kohakutpu.kernels import LOG2E, flash_attention

from kohakutpu import api as ktpu

RULE = "=" * 78
LAYERS, LQ, D, HIDDEN, BLOCK = 2, 64, 64, 64, 64
SCALE = D**-0.5
rng = np.random.default_rng(5)


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def weights():
    """One layer: two norms, four attention projections, two feed-forward."""

    def w(*shape, s=0.25):
        return (rng.standard_normal(shape) * s).astype(np.float16)

    return (
        w(LQ, D, s=0.1) + 1,
        w(D, D),
        w(D, D),
        w(D, D),
        w(D, D),
        w(LQ, D, s=0.1) + 1,
        w(HIDDEN, D),
        w(D, HIDDEN),
    )


def upload(layer):
    """Pin one layer's weights. The softmax scale and log2 e ride on `wq`."""
    return [
        ktpu.tensor((a * SCALE * LOG2E).astype(np.float16) if i == 1 else a).pin()
        for i, a in enumerate(layer)
    ]


def block(x, p):
    """Pre-norm attention, then pre-norm SwiGLU. Reads like torch."""
    g1, wq, wk, wv, wo, g2, w1, w2 = p
    h = x.rmsnorm(g1)
    x = x + flash_attention(h @ wq, h @ wk, wv @ h, block=BLOCK) @ wo
    h = x.rmsnorm(g2)
    return x + (h @ w1).silu() @ w2


def reference(x, layers, eps=1e-5):
    def norm(t, g):
        return t * (1 / np.sqrt((t**2).mean(-1, keepdims=True) + eps)) * g

    for g1, wq, wk, wv, wo, g2, w1, w2 in layers:
        h = norm(x, g1)
        s = (h @ wq.T) @ (h @ wk.T).T * SCALE
        e = np.exp(s - s.max(-1, keepdims=True))
        x = x + (e / e.sum(-1, keepdims=True)) @ (h @ wv.T) @ wo.T
        h = norm(x, g2)
        ff = h @ w1.T
        x = x + (ff / (1 + np.exp(-ff))) @ w2.T
    return x


def forward(host, X):
    """`(result, counters this pass moved)`."""
    on = [upload(layer) for layer in host]
    x = ktpu.tensor(X)
    before = dict(ktpu.stats())
    for p in on:
        x = block(x, p)
    got = x.numpy()
    return got, {k: ktpu.stats()[k] - before[k] for k in before}


dev = ktpu.script_device("sim")
X = rng.standard_normal((LQ, D)).astype(np.float16)
host = [weights() for _ in range(LAYERS)]

head("THE BLOCK, AT THE TENSOR LEVEL")
print("Every line below is a shipped kernel. No tiling is stated anywhere.\n")
for line in (
    "h = x.rmsnorm(g1)",
    "x = x + flash_attention(h @ wq, h @ wk, wv @ h, block=B) @ wo",
    "h = x.rmsnorm(g2)",
    "x = x + (h @ w1).silu() @ w2",
):
    print(f"    {line}")
print(f"\n  {dev}")
print(f"  {LAYERS} layers, L={LQ} D={D} hidden={HIDDEN}")

head("ONE FORWARD PASS")
got, moved = forward(host, X)
want = reference(
    X.astype(np.float64),
    [tuple(a.astype(np.float64) for a in layer) for layer in host],
)
rel = np.abs(got - want) / np.abs(want).max()
params = sum(a.size for layer in host for a in layer)

print(f"  {params:,} parameters, {params * 2:,} bytes of weights")
print(f"  vs float64: p50 {np.median(rel):.2e}   max {rel.max():.2e}\n")
for name in ("dispatches", "rounds", "flits", "relayouts", "sent", "fetched"):
    note = {
        "relayouts": "  <- byte-order rewrites, THROUGH THE HOST",
        "sent": "  bytes to the card",
        "fetched": "  bytes back",
    }.get(name, "")
    print(f"  {name:<12} {moved[name]:>10,}{note}")
print(f"\n  {moved['dispatches'] / LAYERS:.0f} dispatches per layer.")
print(f"  {moved['relayouts']} relayouts is the one to watch: this machine has")
print("  no instruction that reorders memory, so each is a host round trip.")

head("WHERE THE DISPATCHES GO, PER LAYER")
one = upload(weights())
for label, build in (
    ("rmsnorm", lambda p, x: x.rmsnorm(p[0])),
    ("one projection h @ wq", lambda p, x: x @ p[1]),
    (
        "flash_attention",
        lambda p, x: flash_attention(x @ p[1], x @ p[2], p[3] @ x, block=BLOCK),
    ),
    ("silu(h @ w1)", lambda p, x: (x @ p[6]).silu()),
):
    before = dict(ktpu.stats())
    build(one, ktpu.tensor(X)).numpy()
    spent = ktpu.stats()["dispatches"] - before["dispatches"]
    print(f"  {label:<24} {spent:>4} dispatches")
print("\n  Attention dominates. `flash_attention.py` says why: four stages a")
print("  key block, two of them forced by VC -> cluster L1 being shut.")

head("SCALING IN LAYERS")
print(f"  {'layers':>7} {'dispatches':>11} {'sent':>12} {'p50 err':>10}")
for n in (1, 2, 4):
    stack = [weights() for _ in range(n)]
    out, spent = forward(stack, X)
    ref = reference(
        X.astype(np.float64),
        [tuple(a.astype(np.float64) for a in layer) for layer in stack],
    )
    err = float(np.median(np.abs(out - ref) / np.abs(ref).max()))
    print(f"  {n:>7} {spent['dispatches']:>11,} {spent['sent']:>12,} {err:>10.2e}")
print("\n  Linear in layers, as it must be: nothing is cached between them.")

head("WHAT WOULD MAKE THIS A REAL LLM")
print("  HAVE   rmsnorm, matmul, silu/gelu, flash attention with MHA, GQA and")
print("         causal masking, residuals, a per-channel bias, and a tensor")
print("         level that composes them without stating a tiling.\n")
print("  WANT   1. VC -> cluster L1 (HARDWARE-WANTS 1). Today an activation")
print("            between two matmuls MUST go back through MAG, which is the largest")
print("            single cost in the table above.")
print("         2. A KV cache. Every pass here recomputes all of K and V;")
print("            decoding needs them pinned and appended to.")
print("         3. A tokeniser and weight loading -- host code, not a machine")
print("            question, and neither exists yet.")
print("         4. Multi-mesh scheduling: one 4096-wide layer is 314 MB and")
print("            this arena is 16 MB. See demos/kohakutpu/memory.py.")
