"""Dense attention: ``o = softmax(q @ k.T * scale) @ v`` for `heads`
independent heads in one kernel, with v given transposed (``vt``, as a cache
that stores V^T: the clusters read B as [N, K] rows).

Head h is the h-th block of rows of each input: q [heads * S, D], k
[heads * T, D], vt [heads * D, T], o [heads * S, D] -- a batch of heads
(or of (image, head) pairs) is one call. The clusters sweep each head's
scores into a tiled scratch. The cores stream key blocks in 16-row trips,
keeping per-block maxima and sums as compact f16 statistics (16 a word).
They quantise ``exp2(s - max)`` into the second product's MXFP7 A operand;
the clusters sweep each head's ``p @ vt.T``; the cores fold the partial sums,
divide and store o. Causal and other variants are kernels of their own.

This low-level entry consumes physical MXFP7 tiles, not logical f16 tensors.
Its extents must describe complete packed tiles; changing a dimension cannot
insert padding between heads or identify which physical keys are absent.
For partial logical query/key lengths use
``kohakutpu.application.api.attention.attention``: it constructs bounded
per-head forms, supplies the validity mask, and crops the logical output.
The language layer does not import the application runtime or allocate forms.
"""

import kohakutpu.language.dsl as kt
from kohakutpu.language.examples.dsl.cluster.sweep import (
    MAX_TN,
    a_layout,
    b_layout,
    core_blocks,
    nk_for,
    sweep,
)


@kt.kernel
def attention(
    q=kt.In("S", "D", dtype="mx7a", layout=a_layout("tq", "D")),
    k=kt.In("HT", "D", dtype="mx7b", layout=b_layout("tk", "D")),
    vt=kt.In(
        "HD", "T", dtype="mx7b", layout=lambda c: dict(gn=c.D // 4, nk=nk_for(c.T))
    ),
    o=kt.Out("S", "D"),
    *,
    scale=None,
    heads=1,
    tq=64,
    tk=kt.auto(256, 128, 64),
    rows=16,
):
    S, D = q.shape
    T = vt.shape[1]
    kt.require(
        k.shape[0] == heads * T and vt.shape[0] == heads * D,
        f"k {k.shape} and vt {vt.shape}: {heads} heads of {T} keys and width {D}",
    )
    kt.require(
        D % 32 == 0 and D <= MAX_TN,
        f"a head of {D}: whole 32-column blocks up to {MAX_TN}",
    )
    kt.require(T % tk == 0, f"{T} keys in whole blocks of {tk}")
    kt.require(
        tk % (32 * nk_for(T)) == 0, f"key blocks of {tk}: whole K chunks of p @ v"
    )

    kt.require(rows % 16 == 0, "attention statistic trips are whole groups of 16 rows")
    sc = (D**-0.5 if scale is None else scale) * kt.LOG2E
    s = kt.buffer((S, T), "f16", tile=(tq // 4, tk // 4))
    p = kt.buffer((S, T), "mx7a", gm=tq // 4, nk=nk_for(T))
    nb = T // tk
    maxima = kt.buffer((nb, S), "f16")
    row_max = kt.buffer((S,), "f16")
    rs = kt.buffer((nb, S), "f16")
    row_sum = kt.buffer((S,), "f16")
    ov = kt.buffer((S, D), "f16", tile=(tq // 4, D // 4))

    sweep(q, k, s, tq, tk, groups=heads)

    for b in range(nb):
        for r0, _, rper in core_blocks(S, tk, tk, rows, tq):
            for t in kt.range(rper // rows):
                r = r0 + t * rows
                x = kt.load(s[r : r + rows, b * tk : b * tk + tk])
                kt.store(maxima[b, r : r + rows], kt.rmax(x if sc >= 0 else -x))

    for r0, _, rper in core_blocks(S, 32, 32, rows, tq):
        for t in kt.range(rper // rows):
            r = r0 + t * rows
            m = kt.unpack(kt.load(maxima[0, r : r + rows]))
            for j in range(1, nb):
                m = kt.maximum(m, kt.load(maxima[j, r : r + rows]))
            kt.store(row_max[r : r + rows], m)

    for b in range(nb):
        for r0, _, rper in core_blocks(S, tk, tk, rows, tq):
            for t in kt.range(rper // rows):
                r = r0 + t * rows
                m = kt.load(row_max[r : r + rows])
                x = kt.load(s[r : r + rows, b * tk : b * tk + tk]) * sc
                prob = kt.exp2(x - m * abs(sc))
                kt.store(p[r : r + rows, b * tk : b * tk + tk], prob)
                kt.store(rs[b, r : r + rows], kt.rsum(prob))

    for r0, _, rper in core_blocks(S, 32, 32, rows, tq):
        for t in kt.range(rper // rows):
            r = r0 + t * rows
            sums = kt.unpack(kt.load(rs[0, r : r + rows]))
            for b in range(1, nb):
                sums = sums + kt.load(rs[b, r : r + rows])
            kt.store(row_sum[r : r + rows], sums)

    sweep(p, vt, ov, tq, D, groups=heads)

    for r0, _, rper in core_blocks(S, D, D, rows, tq):
        for t in kt.range(rper // rows):
            r = r0 + t * rows
            inv = kt.recip(kt.load(row_sum[r : r + rows]))
            kt.store(o[r : r + rows, :], kt.load(ov[r : r + rows, :]) * inv)


__all__ = ["attention"]
