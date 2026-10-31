"""Row normalisations with per-column weights: each weight is one row loaded
once before the stream loop (a row broadcast every band reads), the row
statistics are register reductions."""

import kohakutpu.language.dsl as kt
from kohakutpu.language.examples.dsl.vector.rows import per_core


@kt.kernel
def layernorm(
    x=kt.In("R", "W"),
    g=kt.In("W"),
    b=kt.In("W"),
    y=kt.Out("R", "W"),
    *,
    eps=1e-5,
    rows=kt.auto(8, 4),
):
    R, W = x.shape
    per = per_core(R, W, rows)
    for c in kt.par(kt.machine().vc, on=kt.vc):
        gl = kt.unpack(kt.load(g, gt4=True))
        bl = kt.unpack(kt.load(b, gt4=True))
        for t in kt.range(per // rows):
            r = c * per + t * rows
            v = kt.load(x[r : r + rows, :], gt4=True)
            d = v - kt.rsum(v) * (1.0 / W)
            inv = kt.rsqrt(kt.rsum(d * d) * (1.0 / W) + eps)
            kt.store(y[r : r + rows, :], d * inv * gl + bl)


@kt.kernel
def rmsnorm(
    x=kt.In("R", "W"), g=kt.In("W"), y=kt.Out("R", "W"), *, eps=1e-6, rows=kt.auto(8, 4)
):
    R, W = x.shape
    per = per_core(R, W, rows)
    for c in kt.par(kt.machine().vc, on=kt.vc):
        gl = kt.unpack(kt.load(g, gt4=True))
        for t in kt.range(per // rows):
            r = c * per + t * rows
            v = kt.load(x[r : r + rows, :], gt4=True)
            inv = kt.rsqrt(kt.rsum(v * v) * (1.0 / W) + eps)
            kt.store(y[r : r + rows, :], v * inv * gl)


__all__ = ["layernorm", "rmsnorm"]
