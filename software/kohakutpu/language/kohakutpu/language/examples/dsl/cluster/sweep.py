"""The pieces every contraction example is built from: an MXFP7 product swept
over the clusters into a tiled scratch, and the vector cores' split of that
scratch into (row block, column block) instances for an epilogue.

Both use every unit of every node (`Machine.total`): on a card whose nodes
share memory through the Xache, a `kt.par` wider than one node spans the
nodes, so the same kernel runs on one node or four with no cross-node code
in it -- the compiler orders the nodes where one reads what another wrote.
"""

from collections.abc import Iterator

import kohakutpu.language.dsl as kt

#: Widest column block one band's registers hold.
MAX_TN = 256


def nk_for(k: int) -> int:
    """K-blocks of 32 a chunk: 2 where K splits into 64s, else 1. A port's
    packing and the sweep both read it from K, so they agree."""
    return 2 if k % 64 == 0 else 1


def a_layout(tm_key: str = "tm", k_dim: str = "K"):
    """An mx7a port's packing: tiles of `tm` rows, chunks from K."""
    return lambda k: dict(gm=getattr(k, tm_key) // 4, nk=nk_for(getattr(k, k_dim)))


def b_layout(tn_key: str = "tn", k_dim: str = "K"):
    """An mx7b port's packing: tiles of `tn` rows, chunks from K."""
    return lambda k: dict(gn=getattr(k, tn_key) // 4, nk=nk_for(getattr(k, k_dim)))


def units(kind: str, wanted: int) -> int:
    """Instances of `kind` for `wanted` pieces of work: at most every unit
    of every node, a node's worth at a time once past one node."""
    m = kt.machine()
    per = getattr(m, kind)
    n = min(m.total(kind), wanted)
    return n if n <= per else -(-n // per) * per


def sweep(
    a: kt.Param, b: kt.Param, h: kt.Param, tm: int, tn: int, groups: int = 1
) -> None:
    """``h = a @ b.T``: [tm, tn] tiles spread over the clusters, K in chunks
    of 32 * nk_for(K), each tile drained into the tiled scratch `h`. With
    `groups` G, a and b are G stacked blocks of rows and block g of h is
    a_g @ b_g.T: h is [M, N / G] (one head of attention a group)."""
    M, K = a.shape
    N = b.shape[0] // groups
    kc = 32 * nk_for(K)
    kt.require(
        b.shape[0] % groups == 0 and M % groups == 0,
        f"{M} and {b.shape[0]} rows in {groups} groups",
    )
    Mg = M // groups
    rows = f"{Mg} rows" + (" a group" if groups > 1 else "")
    kt.require(Mg % tm == 0 and tm % 4 == 0, f"{rows} in whole tiles of {tm}")
    kt.require(N % tn == 0 and tn % 32 == 0, f"{N} columns in whole tiles of {tn}")
    kt.require(K % kc == 0, f"K = {K} in whole chunks of {kc}")

    ntn = N // tn
    tiles = (M // tm) * ntn
    per_group = (Mg // tm) * ntn
    ncl = units("mg", tiles)
    per = -(-tiles // ncl)

    def tile(t):
        ti, tj = t // ntn, t % ntn
        nb = tj * tn
        if groups > 1:
            nb = (t // per_group) * N + nb  # this group's block of b
        acc = kt.zero(tm, tn)
        for k in kt.range(K // kc):
            fa = kt.fill(a[ti * tm : ti * tm + tm, k * kc : k * kc + kc])
            fb = kt.fill(b[nb : nb + tn, k * kc : k * kc + kc])
            acc = kt.gemm(fa, fb, acc)
        kt.drain(acc, h[ti * tm : ti * tm + tm, tj * tn : tj * tn + tn])

    for u in kt.par(ncl, on=kt.mg):
        for i in kt.range(per):
            t = u + ncl * i
            if per * ncl == tiles:
                tile(t)
            else:
                with kt.when(t < tiles):  # the last sweep is short
                    tile(t)


def core_blocks(
    M: int, N: int, tn: int, rows: int, tm: int | None = None
) -> Iterator[tuple]:
    """``(first row, first column, rows)`` of each vector-core instance: the
    rows split over every core, at most `tm` (the tile height of a tiled
    scratch it reads: a band's address is affine inside one tile row) an
    instance, column blocks of `tn` beside them. Instances past the cores
    are placed by the place pass (`at=kt.ANY`)."""
    m = kt.machine()
    cores = m.total("vc")
    kt.require(
        tn <= MAX_TN and N % tn == 0,
        f"{N} columns in blocks of {tn} (at most {MAX_TN})",
    )
    rper = M // cores if tm is None else min(M // cores, tm)
    kt.require(
        rper > 0 and M % rper == 0 and rper % rows == 0,
        f"{M} rows in instances of {rper} x trips of {rows}",
    )
    kt.require(
        tm is None or tm % rper == 0, f"instances of {rper} rows inside tiles of {tm}"
    )
    nrb, ntn = M // rper, N // tn
    anywhere = nrb * ntn > cores
    for uv in kt.par(nrb * ntn, on=kt.vc, at=kt.ANY if anywhere else None):
        rb, cb = (uv % nrb, uv // nrb) if ntn > 1 else (uv, 0)
        yield rb * rper, cb * tn, rper


__all__ = ["MAX_TN", "a_layout", "b_layout", "core_blocks", "nk_for", "sweep", "units"]
