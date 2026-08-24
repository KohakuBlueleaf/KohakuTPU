"""The `@kernel` code SDXL needs that the library does not already ship.

Each one is bridged to tinygrad at the bottom of the file, so a layer in
`nn.py` calls it as an ordinary `Tensor -> Tensor` function and never sees a
device tensor.

`geglu` is the one that earns its keep: SDXL's FeedForward is
`gelu(x @ Wg) * (x @ Wp)`, which is two GEMMs and an elementwise pass, and the
compiler now fuses that onto ONE vector core with no temp and no MAG round
trip. Written as two tinygrad matmuls it is two dispatches and a buffer.
"""

import ktpugrad
from kohakuaccel.lang import dims, loop, units
from kohakutpu.lang import kernel

from kohakutpu import lang as L

M, K, N = dims("M, K, N")

#: `exp2` is the transcendental this core has, so every exponential folds the
#: base change into a constant rather than spending a pass on it.
LOG2E = 1.4426950408889634

#: tanh-free GELU, the approximation SDXL's checkpoints were trained against.
GELU_IN = 0.7978845608028654
GELU_CUBE = 0.044715


@kernel
def silu_fn(x=L.In(..., M, N), y=L.Out(..., M, N), *, part=8192):
    """`x * sigmoid(x)` in one vector pass, no intermediate in memory."""
    with units(x.parts(part)) as e:
        y[e] <<= x[e] * L.recip(L.exp2(x[e] * -LOG2E) + 1.0)


@kernel
def gelu_fn(x=L.In(..., M, N), y=L.Out(..., M, N), *, part=8192):
    """GELU, tanh approximation, as `2 * sigmoid(2u) * x` with `u` the cubic."""
    with units(x.parts(part)) as e:
        u = GELU_IN * (x[e] + GELU_CUBE * x[e] * x[e] * x[e])
        y[e] <<= x[e] * L.recip(L.exp2(u * (-2.0 * LOG2E)) + 1.0)


@kernel
def geglu_combine_fn(
    value=L.In(..., M, N), gate=L.In(..., M, N), y=L.Out(..., M, N), *, part=8192
):
    """`value * gelu(gate)` -- SDXL's GEGLU after its projection is chunked.

    `sdxl_original_unet.py:576` is `hidden, gate = proj(x).chunk(2, -1)`, so the
    FIRST half is the value and the second is the gate. One vector pass.
    """
    with units(value.parts(part)) as e:
        g = gate[e]
        u = GELU_IN * (g + GELU_CUBE * g * g * g)
        y[e] <<= value[e] * (g * L.recip(L.exp2(u * (-2.0 * LOG2E)) + 1.0))


@kernel
def geglu_fused_fn(
    x=L.In(..., M, K),
    value_w=L.In(N, K),
    gate_w=L.In(N, K),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
):
    """The same thing with the projection SPLIT into two GEMMs and fused.

    Mathematically identical to one `2N`-wide Linear and a chunk, but the `2N`
    intermediate never exists: both tiles drain into one vector core. **Bias
    free** -- an epilogue admits one non-accumulator operand and this wants two,
    one per half, so a biased GEGLU still goes the `geglu_combine` road.
    """
    with units(x.tiles(gm), value_w.tiles(gn)) as (i, j):
        v = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            v += x[i, k] @ value_w[j, k]
        g = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            g += x[i, k] @ gate_w[j, k]
        u = GELU_IN * (g + GELU_CUBE * g * g * g)
        y[i, j] <<= v * (g * L.recip(L.exp2(u * (-2.0 * LOG2E)) + 1.0))


#: The tinygrad-facing forms. `bridge` marshals Tensors in and out, so a layer
#: never touches a device tensor.
silu = ktpugrad.bridge(silu_fn)
gelu = ktpugrad.bridge(gelu_fn)
geglu_combine = ktpugrad.bridge(geglu_combine_fn)
geglu_fused = ktpugrad.bridge(geglu_fused_fn)
