"""Bigger kernels in the DSL, checked against numpy.

    python examples/03_write_a_real_kernel.py

A transformer FFN, single-head attention, and a fused GEMM epilogue -- all
ordinary Python over tracers, all verified by the level-1 reference simulator.
"""

import numpy as np

import ktpu.dsl as D
from ktpu.interp import run_one
from ktpu.ir import FP16

RNG = np.random.default_rng(0)
B, T, C, H = 2, 16, 64, 256


def spec(*shape):
    return D.TensorSpec(shape, FP16)


def ffn(x, w1, w2):
    """SwiGLU-free FFN: `silu(x @ w1) @ w2`."""
    return D.silu(x @ w1) @ w2


def swiglu_ffn(x, wg, wu, wd):
    """`(silu(x @ wg) * (x @ wu)) @ wd`, the gated form."""
    return (D.silu(x @ wg) * (x @ wu)) @ wd


def attention(q, k, v, scale: float):
    """Single-head scaled dot-product attention, no mask."""
    return D.softmax((q @ k.transpose()) * scale) @ v


def gemm_bias_gelu(x, w, b):
    """One matmul with its whole epilogue fused: `gelu(x @ w + b)`."""
    return D.gelu(x @ w + b)


print("=== FFN: silu(x @ w1) @ w2 ===")
g = D.trace(ffn, spec(T, C), spec(C, H), spec(H, C))
print(f"    {len(g.ops)} ops, {sum(o.kind.value == 'matmul' for o in g.ops)} matmuls")

x, w1, w2 = RNG.normal(size=(T, C)), RNG.normal(size=(C, H)), RNG.normal(size=(H, C))
got = run_one(g, x, w1, w2)
h = x @ w1
want = (h / (1.0 + np.exp(-h))) @ w2
print(f"    max abs err vs numpy: {np.abs(got - want).max():.3e}")

print("\n=== SwiGLU FFN, three matmuls sharing one input ===")
g = D.trace(swiglu_ffn, spec(T, C), spec(C, H), spec(C, H), spec(H, C))
print(f"    {len(g.ops)} ops, {sum(o.kind.value == 'matmul' for o in g.ops)} matmuls")

print("\n=== attention: softmax(q k^T * scale) v ===")
g = D.trace(attention, spec(T, C), spec(T, C), spec(T, C), scale=C**-0.5)
print(f"    {len(g.ops)} ops")
q, k, v = (RNG.normal(size=(T, C)) for _ in range(3))
got = run_one(g, q, k, v)
s = (q @ k.T) * C**-0.5
e = np.exp(s - s.max(-1, keepdims=True))
want = (e / e.sum(-1, keepdims=True)) @ v
print(f"    max abs err vs numpy: {np.abs(got - want).max():.3e}")

print("\n=== gemm + bias + gelu, one fused epilogue ===")
g = D.trace(
    gemm_bias_gelu,
    spec(T, C),
    spec(C, H),
    spec(
        H,
    ),
)
kinds = [o.kind.value for o in g.ops]
print(f"    {len(g.ops)} ops: 1 matmul then {len(g.ops) - kinds.count('input') - 1}")
print("    the epilogue is elementwise, so level 2 can fuse it onto the matmul")

print("\n=== the same kernel at another shape is another graph ===")
for t in (16, 64):
    gg = D.trace(
        gemm_bias_gelu,
        spec(t, C),
        spec(C, H),
        spec(
            H,
        ),
    )
    print(f"    T={t:>3}: out {gg.outputs[0].shape}, {len(gg.ops)} ops")
