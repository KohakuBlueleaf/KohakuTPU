"""Dense attention: ``o = softmax(q @ k.T * scale) @ v`` for `heads`
independent heads in one kernel, with v given transposed (``vt``, as a cache
that stores V^T: the clusters read B as [N, K] rows).

Head h is the h-th block of rows of each input: q [heads * S, D], k
[heads * T, D], vt [heads * D, T], o [heads * S, D] -- a batch of heads
(or of (image, head) pairs) is one call. The clusters sweep each head's
scores into a tiled scratch; the cores take each band's row max and sum over
all key blocks (a Python loop: unrolled, every block of the band in L1 at
once), quantise ``exp2(s - max)`` into the second product's MXFP7 A operand
and keep the row sums as an [S, 32] statistic; the clusters sweep each
head's ``p @ vt.T``; the cores divide by the sums and store o. Causal and
other variants are kernels of their own."""

import kohakutpu.language.dsl as kt
from kohakutpu.language.examples.dsl.cluster.sweep import (
    MAX_TN,
    a_layout,
    b_layout,
    core_blocks,
    nk_for,
    sweep,
)

#: Key blocks a band holds in one core's L1 at once.
MAX_BLOCKS = 3


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
    rows=4,
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
    kt.require(
        T % tk == 0 and T // tk <= MAX_BLOCKS,
        f"{T} keys in at most {MAX_BLOCKS} blocks of {tk}",
    )
    kt.require(
        tk % (32 * nk_for(T)) == 0, f"key blocks of {tk}: whole K chunks of p @ v"
    )

    sc = (D**-0.5 if scale is None else scale) * kt.LOG2E
    s = kt.buffer((S, T), "f16", tile=(tq // 4, tk // 4))
    p = kt.buffer((S, T), "mx7a", gm=tq // 4, nk=nk_for(T))
    rs = kt.buffer((S, 32), "f16")  # each row's sum, in all 32 columns
    ov = kt.buffer((S, D), "f16", tile=(tq // 4, D // 4))

    sweep(q, k, s, tq, tk, groups=heads)

    nb = T // tk
    for r0, _, rper in core_blocks(S, tk, tk, rows, tq):
        for t in kt.range(rper // rows):
            r = r0 + t * rows
            blocks = [
                kt.load(s[r : r + rows, b * tk : b * tk + tk]) * sc for b in range(nb)
            ]

            m = kt.rmax(blocks[0])
            for x in blocks[1:]:
                m = kt.maximum(m, kt.rmax(x))

            ps = [kt.exp2(x - m) for x in blocks]
            total = kt.rsum(ps[0])
            for x in ps[1:]:
                total = total + kt.rsum(x)

            for b, x in enumerate(ps):
                kt.store(p[r : r + rows, b * tk : b * tk + tk], x)  # packed to MXFP7
            kt.store(rs[r : r + rows, :], total)

    sweep(p, vt, ov, tq, D, groups=heads)

    for r0, _, rper in core_blocks(S, D, D, rows, tq):
        for t in kt.range(rper // rows):
            r = r0 + t * rows
            inv = kt.recip(kt.load(rs[r : r + rows, :], gt4=True))
            kt.store(o[r : r + rows, :], kt.load(ov[r : r + rows, :]) * inv)


__all__ = ["MAX_BLOCKS", "attention"]
