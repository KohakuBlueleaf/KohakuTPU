"""The row-wise kernels, in the shape one tiling with no checkpoint argues for.

Each is ONE stage with every intermediate in a v-register and none in memory.
Measured at 64x64 against the staged forms they replaced: softmax 4 stages/974
words -> 1/290, rmsnorm 4/647 -> 1/264, layernorm and group_norm 8/1254 ->
1/416, at equal or lower error.

`part` must be WHOLE ROWS -- a band steps one row at a time when it reduces --
but not the whole array: rows split across instances fold identically, so the
default grids as the staged forms did. A part that cuts a row is refused and
the message names the knob.

THESE ARE THE SHIPPED KERNELS, exported as `softmax`, `rmsnorm`, `layernorm`
and `group_norm`. Only `group_norm_staged` is still library code; the other
three staged forms are fixtures in `compiler/tests/test_fused_shape.py`.
Promoting them moved four witness digests.
"""

from kohakuaccel.lang import dims, units
from kohakutpu.lang import kernel
from kohakutpu.ops import LOG2E

from kohakutpu import lang as L

M, N = dims("M, N")


@kernel
def softmax_fused(x=L.In(..., M, N), y=L.Out(..., M, N), *, part=8192):
    """Row-wise softmax in ONE tiling, with no intermediate in memory.

    Two chains: `ex` is read twice, so it is computed once into a register and
    the sum and the divide both take it from there. Six of eight descriptors,
    with `log2 e` still filled from DRAM -- the budget can afford it.
    """
    with units(x.parts(part)) as e:
        ex = L.exp2((x[e] - L.row_max(x[e])) * LOG2E)
        y[e] <<= ex / L.row_sum(ex)


@kernel
def rmsnorm_fused(
    x=L.In(..., M, N), w=L.In(..., M, N), y=L.Out(..., M, N), *, eps=1e-5, part=8192
):
    """``x * rsqrt(mean(x^2) + eps) * w`` in one tiling.

    The shipped form divides before squaring because the sum reaches fp16's
    ceiling; here the sum is an E8M15 accumulator and never lands in memory, so
    the scaling is a lowering decision rather than the author's.

    Already linear, so it is ONE chain -- what it needed was `N` and `eps` in
    registers, which takes it from ten descriptors to six.
    """
    with units(x.parts(part)) as e:
        ms = L.row_sum((x[e] / N) * x[e])
        y[e] <<= x[e] * L.rsqrt(ms + eps) * w[e]


@kernel
def layernorm_fused(
    x=L.In(..., M, N),
    w=L.In(..., M, N),
    b=L.In(..., M, N),
    y=L.Out(..., M, N),
    *,
    eps=1e-5,
    part=8192,
):
    """``(x - mean) * rstd * w + b`` in one tiling, two reductions deep.

    The second reduction reads the first's result, which is what makes this the
    hardest of the four: a band has to carry a value ACROSS a `VRED`. `dev` is
    read three times, so it is one lifted chain held in a register throughout.

    Exactly 8 of 8 descriptors, with no margin: `N` and `eps` behind MAG would
    need 12.
    """
    with units(x.parts(part)) as e:
        mu = L.row_sum(x[e]) / N
        dev = x[e] - mu
        var = L.row_sum((dev / N) * dev)
        y[e] <<= dev * L.rsqrt(var + eps) * w[e] + b[e]


@kernel
def group_norm_fused(
    x=L.In(..., M, N),
    w=L.In(..., M, N),
    b=L.In(..., M, N),
    y=L.Out(..., M, N),
    *,
    eps=1e-5,
    part=8192,
):
    """GroupNorm over rows, one group per row, in one tiling.

    Same shape as :func:`layernorm_fused`; kept separate because the group
    version is what SDXL calls and its row is the group, not the channel axis.
    """
    with units(x.parts(part)) as e:
        mu = L.row_sum(x[e]) / N
        dev = x[e] - mu
        var = L.row_sum((dev / N) * dev)
        y[e] <<= dev * L.rsqrt(var + eps) * w[e] + b[e]
