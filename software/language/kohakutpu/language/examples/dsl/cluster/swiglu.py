"""SwiGLU feed-forward, ``y = (act(x @ w1.T) * (x @ w3.T)) @ w2.T``: the gate
and up products swept into tiled scratches; the cores form ``act(g) * u``
a band at a time and quantise it into the down product's MXFP7 A operand;
the down product swept; the cores store y row-major. ``act="gelu"`` is
GeGLU."""

import kohakutpu.language.dsl as kt
from kohakutpu.language.examples.dsl.acts import act as pick
from kohakutpu.language.examples.dsl.cluster.sweep import (
    a_layout,
    b_layout,
    core_blocks,
    nk_for,
    sweep,
)


@kt.kernel
def swiglu(
    x=kt.In("M", "D", dtype="mx7a", layout=a_layout(k_dim="D")),
    w1=kt.In("H", "D", dtype="mx7b", layout=b_layout("tn1", "D")),
    w3=kt.In("H", "D", dtype="mx7b", layout=b_layout("tn1", "D")),
    w2=kt.In("O", "H", dtype="mx7b", layout=b_layout("tn2", "H")),
    y=kt.Out("M", "O"),
    *,
    act="silu",
    tm=64,
    tn1=kt.auto(256, 128, 64),
    tn2=kt.auto(256, 128, 64, 32),
    rows=kt.auto(8, 4),
):
    M = x.shape[0]
    H, O = w1.shape[0], w2.shape[0]
    f = pick(act)
    kt.require(
        tn1 % (32 * nk_for(H)) == 0,
        f"hidden blocks of {tn1}: whole K chunks of the down product",
    )
    g = kt.buffer((M, H), "f16", tile=(tm // 4, tn1 // 4))
    u = kt.buffer((M, H), "f16", tile=(tm // 4, tn1 // 4))
    a2 = kt.buffer((M, H), "mx7a", gm=tm // 4, nk=nk_for(H))
    h = kt.buffer((M, O), "f16", tile=(tm // 4, tn2 // 4))
    sweep(x, w1, g, tm, tn1)
    sweep(x, w3, u, tm, tn1)
    for r0, col, rper in core_blocks(M, H, tn1, rows, tm):
        for t in kt.range(rper // rows):
            r = r0 + t * rows
            gv = kt.load(g[r : r + rows, col : col + tn1])
            uv = kt.load(u[r : r + rows, col : col + tn1])
            kt.store(a2[r : r + rows, col : col + tn1], f(gv) * uv)
    sweep(a2, w2, h, tm, tn2)
    for r0, col, rper in core_blocks(M, O, tn2, rows, tm):
        for t in kt.range(rper // rows):
            r = r0 + t * rows
            kt.store(
                y[r : r + rows, col : col + tn2],
                kt.load(h[r : r + rows, col : col + tn2]),
            )


__all__ = ["swiglu"]
