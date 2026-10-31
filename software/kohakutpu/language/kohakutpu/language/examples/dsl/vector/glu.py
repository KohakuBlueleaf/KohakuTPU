"""Gated linear unit tail: ``y = a * act(g)`` (SwiGLU with act="silu",
GeGLU with "gelu"), two row operands streamed side by side."""

import kohakutpu.language.dsl as kt
from kohakutpu.language.examples.dsl.acts import act as pick
from kohakutpu.language.examples.dsl.vector.rows import per_core


@kt.kernel
def glu(
    a=kt.In("R", "W"),
    g=kt.In("R", "W"),
    y=kt.Out("R", "W"),
    *,
    act="silu",
    rows=kt.auto(8, 4),
):
    R, W = a.shape
    per = per_core(R, W, rows)
    f = pick(act)
    for c in kt.par(kt.machine().vc, on=kt.vc):
        for t in kt.range(per // rows):
            r = c * per + t * rows
            av = kt.load(a[r : r + rows, :], gt4=True)
            gv = kt.load(g[r : r + rows, :], gt4=True)
            kt.store(y[r : r + rows, :], av * f(gv))


__all__ = ["glu"]
