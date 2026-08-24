"""Gated feed-forward: two projections over ONE operand stream.

SDXL's and a modern LLM's feed forward is `act(x @ wg.T) * (x @ wu.T)`. Both
halves read the same `x`, so run as two kernels the second refetches all of it.

Here they share a cluster and `x` stays in L1 -- the backend drops a FILL of
what is already resident. Measured at 64x128x64, gm=gn=8, nk=2: SIX fills where
two separate projections need eight.

SEQUENTIALLY, not interleaved: a cluster has ONE accumulator. Interleaving the
sweeps compiles, runs, and returns a 123% error, because the second GEMM's
reset destroys the first tile.
"""

from kohakuaccel.lang import dims, loop, units
from kohakutpu.lang import kernel

from kohakutpu import lang as L

M, K, N, F = dims("M, K, N, F")

#: log2(e). The core has `exp2` and no `exp`.
LOG2E = 1.4426950408889634


def _act(v, kind: str | None):
    """`kind` applied to a resident tile, or the tile itself when it is None.

    A COMPILE-TIME branch: `kind` is an ordinary Python knob, so the unchosen
    arms are never traced and cost nothing.
    """
    if kind is None:
        return v
    if kind == "silu":
        return v * L.recip(L.exp2(v * -LOG2E) + 1.0)
    if kind == "gelu":
        return v * L.recip(L.exp2(v * (-1.702 * LOG2E)) + 1.0)
    if kind == "relu":
        return L.maximum(v, 0.0)
    raise ValueError(f"act must be silu, gelu, relu or None, not {kind!r}")


@kernel
def mlp(
    x=L.In(..., M, K),
    up=L.In(F, K),
    down=L.In(N, F),
    y=L.Out(..., M, N),
    *,
    act: str | None = "silu",
    gm=8,
    gn=8,
    nk=2,
):
    """``down(act(up(x)))``. `act` is silu, gelu, relu, or None for a plain pair.

    TWO stages, and two is the floor: `h` has to go back through MAG because the return
    leg a vector core would need -- VC into a cluster's L1 -- cannot carry the
    format -- L1 cannot carry MXFP7. What IS saved is the activation's
    own pass: it rides the first contraction's resident tile as it drains.
    """
    h = L.temp(M, F)
    with units(x.tiles(gm), up.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ up[j, k]
        h[i, j] <<= _act(acc, act)

    with units(h.tiles(gm), down.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(h.chunks32(nk)):
            acc += h[i, k] @ down[j, k]
        y[i, j] <<= acc


@kernel
def geglu(
    x=L.In(..., M, K),
    wg=L.In(N, K),
    wu=L.In(N, K),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
    part=8192,
):
    """``gelu(x @ wg.T) * (x @ wu.T)``. The GELU rides the gate's resident tile.

    `wg` is the gate projection and `wu` the up projection, both ``[N][K]`` as
    a torch Linear keeps them. GATED ON `wg`: a caller whose checkpoint chunks
    the projection has to know which half gates -- SDXL's `chunk(2)` gates on the
    SECOND, so its halves go in swapped.

    TWO GRIDS, and they may NOT be merged to share the `x` fill. Two accumulator
    tiles in ONE grid return partial sums once the sweep is three K-chunks or
    more: MEASURED at `64x256x256`, p50 3.6e-4 and max 1.6e-2 as written against
    p50 1.2e-2 and max 8.8e-1 merged, with 8.8% of elements past 10% -- and SDXL
    is 10 to 20 chunks. Correct at one and two chunks, which is the only reason
    it was ever measured clean.
    """
    gate, up = L.temp(M, N), L.temp(M, N)
    with units(x.tiles(gm), wg.tiles(gn)) as (i, j):
        g = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            g += x[i, k] @ wg[j, k]
        gate[i, j] <<= g * L.recip(L.exp2(g * (-1.702 * LOG2E)) + 1.0)

    with units(x.tiles(gm), wu.tiles(gn)) as (i, j):
        u = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            u += x[i, k] @ wu[j, k]
        up[i, j] <<= u

    with units(gate.parts(part)) as e:
        y[e] <<= gate[e] * up[e]


@kernel
def swiglu(
    x=L.In(..., M, K),
    wg=L.In(N, K),
    wu=L.In(N, K),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
    part=8192,
):
    """``silu(x @ wg.T) * (x @ wu.T)``. :func:`geglu` with the other gate.

    TWO GRIDS for the reason :func:`geglu` gives: two accumulators in one grid
    return partial sums past two K-chunks.
    """
    gate, up = L.temp(M, N), L.temp(M, N)
    with units(x.tiles(gm), wg.tiles(gn)) as (i, j):
        g = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            g += x[i, k] @ wg[j, k]
        gate[i, j] <<= g * L.recip(L.exp2(g * -LOG2E) + 1.0)

    with units(x.tiles(gm), wu.tiles(gn)) as (i, j):
        u = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            u += x[i, k] @ wu[j, k]
        up[i, j] <<= u

    with units(gate.parts(part)) as e:
        y[e] <<= gate[e] * up[e]
