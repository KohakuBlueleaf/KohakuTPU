"""Float32 numpy references, and the grading every layer is held to.

A wrong result has to be localised to the layer that produced it, so every
module in this demo is graded against the arithmetic here rather than against
the end of the pipeline.
"""

import numpy as np
from tinygrad.tensor import Tensor

#: MXFP7 through a contraction lands here; an elementwise pass lands near 1e-4.
CONTRACTION = 5e-2
ELEMENTWISE = 1e-3

GELU_IN = 0.7978845608028654
GELU_CUBE = 0.044715


def host(x) -> np.ndarray:
    """Anything -- Tensor or array -- as a float32 host array."""
    return np.asarray(x.numpy() if isinstance(x, Tensor) else x, np.float32)


def grade(name: str, got, want, bound: float = CONTRACTION) -> float:
    """Relative error of `got` against `want`, printed and returned.

    Scaled by the reference's peak rather than per element: a per-element
    relative error is unbounded wherever the reference is near zero.
    """
    a, b = host(got), host(want)
    if a.shape != b.shape:
        raise ValueError(f"{name}: got {a.shape}, reference is {b.shape}")
    rel = np.abs(a - b).max() / max(np.abs(b).max(), 1e-9)
    mark = "ok" if rel < bound else "OFF"
    print(f"  {name:<34} rel {rel:.3e}  {mark}")
    return rel


def silu(x: np.ndarray) -> np.ndarray:
    return x / (1.0 + np.exp(-x))


def gelu(x: np.ndarray) -> np.ndarray:
    """The tanh approximation, written as a sigmoid to match the kernel."""
    u = GELU_IN * (x + GELU_CUBE * x**3)
    return x / (1.0 + np.exp(-2.0 * u))


def layernorm(x: np.ndarray, w: np.ndarray, b: np.ndarray, eps: float = 1e-5):
    mu = x.mean(axis=-1, keepdims=True)
    var = ((x - mu) ** 2).mean(axis=-1, keepdims=True)
    return (x - mu) / np.sqrt(var + eps) * w + b


def linear(x: np.ndarray, w: np.ndarray, b: np.ndarray | None = None):
    out = x @ w.T
    return out if b is None else out + b


def geglu_fused(x: np.ndarray, value_w: np.ndarray, gate_w: np.ndarray):
    """The split form: value half times gelu of the gate half."""
    return (x @ value_w.T) * gelu(x @ gate_w.T)


def attention(q: np.ndarray, k: np.ndarray, v: np.ndarray) -> np.ndarray:
    """`softmax(q @ k.T / sqrt(d)) @ v`, per head, in float32."""
    scores = q @ np.swapaxes(k, -1, -2) / np.sqrt(q.shape[-1])
    scores = scores - scores.max(axis=-1, keepdims=True)
    weights = np.exp(scores)
    return (weights / weights.sum(axis=-1, keepdims=True)) @ v
