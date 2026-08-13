"""Normalisations, NO AFFINE -- that is what makes each an op and not a kernel.

The weight and bias are a separate FMA at this level. Folded in, or fused to
the linear beside them, they are `kernels.rmsnorm` and friends.
"""

from kohakuaccel.lang import dims, units
from kohakutpu.lang import kernel

from kohakutpu import lang as L

M, N = dims("M, N")

#: log2(e). The core has `exp2` and no `exp`.
LOG2E = 1.4426950408889634


@kernel
def softmax(x=L.In(..., M, N), y=L.Out(..., M, N), *, part=8192):
    """Row-wise softmax, direct.

    `ex` is read twice, so it is computed once into a register and the sum and
    the divide both take it from there. For a row wider than one `VRED` pass,
    or one streamed against a running maximum, reach for `kernels.softmax`.
    """
    with units(x.parts(part)) as e:
        ex = L.exp2((x[e] - L.row_max(x[e])) * LOG2E)
        y[e] <<= ex / L.row_sum(ex)


@kernel
def rmsnorm(x=L.In(..., M, N), y=L.Out(..., M, N), *, eps=1e-5, part=8192):
    """``x * rsqrt(mean(x^2) + eps)``, NO affine.

    Divided before squaring because the sum reaches fp16's ceiling: RMS 30 over
    64 lanes sums to 57600 against a limit of 65504, and an infinite sum makes
    `rsqrt` return zero.
    """
    with units(x.parts(part)) as e:
        ms = L.row_sum((x[e] / N) * x[e])
        y[e] <<= x[e] * L.rsqrt(ms + eps)


@kernel
def layernorm(x=L.In(..., M, N), y=L.Out(..., M, N), *, eps=1e-5, part=8192):
    """``(x - mean) * rstd``, NO affine.

    Two reductions deep, the second reading the first: `dev` is read three
    times and stays in a register throughout.
    """
    with units(x.parts(part)) as e:
        mu = L.row_sum(x[e]) / N
        dev = x[e] - mu
        var = L.row_sum((dev / N) * dev)
        y[e] <<= dev * L.rsqrt(var + eps)
