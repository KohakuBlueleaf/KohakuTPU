"""Two-layer MLP, ``y = act(x @ w1.T + b1) @ w2.T + b2``, as one kernel: the
first product swept into a tiled scratch; the cores add b1, apply the
activation and quantise each band straight into the second product's MXFP7
A operand (the hidden activation never exists as f16 in memory); the second
product swept; the cores add b2 and store y."""

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
def mlp(
    x=kt.In("M", "D", dtype="mx7a", layout=a_layout(k_dim="D")),
    w1=kt.In("H", "D", dtype="mx7b", layout=b_layout("tn1", "D")),
    b1=kt.In("H"),
    w2=kt.In("O", "H", dtype="mx7b", layout=b_layout("tn2", "H")),
    b2=kt.In("O"),
    y=kt.Out("M", "O"),
    *,
    act="gelu",
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
        f"hidden blocks of {tn1}: whole K chunks of the second product",
    )
    h1 = kt.buffer((M, H), "f16", tile=(tm // 4, tn1 // 4))
    a2 = kt.buffer((M, H), "mx7a", gm=tm // 4, nk=nk_for(H))
    h2 = kt.buffer((M, O), "f16", tile=(tm // 4, tn2 // 4))
    sweep(x, w1, h1, tm, tn1)
    for r0, col, rper in core_blocks(M, H, tn1, rows, tm):
        bl = kt.unpack(kt.load(b1[col : col + tn1], gt4=True))
        for t in kt.range(rper // rows):
            r = r0 + t * rows
            v = kt.load(h1[r : r + rows, col : col + tn1])
            kt.store(a2[r : r + rows, col : col + tn1], f(v + bl))  # packed to MXFP7
    sweep(a2, w2, h2, tm, tn2)
    for r0, col, rper in core_blocks(M, O, tn2, rows, tm):
        bl = kt.unpack(kt.load(b2[col : col + tn2], gt4=True))
        for t in kt.range(rper // rows):
            r = r0 + t * rows
            v = kt.load(h2[r : r + rows, col : col + tn2])
            kt.store(y[r : r + rows, col : col + tn2], v + bl)


__all__ = ["mlp"]
