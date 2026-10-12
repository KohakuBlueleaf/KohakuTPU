"""``y = act(x @ w.T + bias)`` as one kernel: the clusters sweep the product
into a tiled scratch, the vector cores read it back a band at a time, add
the bias row, apply the activation and store row-major f16."""

import kohakutpu.language.dsl as kt
from kohakutpu.language.examples.dsl.acts import act as pick
from kohakutpu.language.examples.dsl.cluster.sweep import (
    a_layout,
    b_layout,
    core_blocks,
    sweep,
)


@kt.kernel
def linear(
    x=kt.In("M", "K", dtype="mx7a", layout=a_layout()),
    w=kt.In("N", "K", dtype="mx7b", layout=b_layout()),
    bias=kt.In("N"),
    y=kt.Out("M", "N"),
    *,
    act="none",
    tm=64,
    tn=kt.auto(256, 128, 64, 32),
    rows=kt.auto(8, 4),
):
    M = x.shape[0]
    N = w.shape[0]
    f = pick(act)
    h = kt.buffer((M, N), "f16", tile=(tm // 4, tn // 4))
    sweep(x, w, h, tm, tn)
    for r0, col, rper in core_blocks(M, N, tn, rows, tm):
        bl = kt.unpack(kt.load(bias[col : col + tn], gt4=True))
        for t in kt.range(rper // rows):
            r = r0 + t * rows
            v = kt.load(h[r : r + rows, col : col + tn])
            kt.store(y[r : r + rows, col : col + tn], f(v + bl))


__all__ = ["linear"]
