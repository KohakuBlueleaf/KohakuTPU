"""Hand-written L1 matmul with a vector epilogue, ``silu(a @ b.T)``, overlapped
in one package.

The clusters run `matmul.cluster_ops`; each drained tile is a `mark` in its
cluster's stream. A vector core runs the epilogue over a tile in memory, in
place, as one `stream` pipeline, once the node has waited up to that tile's
mark. The node takes those waits one round of tiles behind the sends, so every
cluster already holds its next tile when the node blocks; tiles go to the
cores in drain order, round robin. The epilogue runs from memory, not from a
cluster-to-core drain: a drain always starts at sub-tile 0 and a core's L1
holds 512 words, so a direct transfer caps the tile at 16x32 sub-tiles.
"""

from kohakutpu.ir.l1.kernels import matmul as MM
from kohakutpu.ir.l1.kernels import silu as SI
from kohakutpu.ir.l1.kernels import stream


def matmul_silu(
    prog,
    a_at,
    b_at,
    c_at,
    m,
    n,
    k,
    gm,
    gn,
    nk,
    late=False,
    stagger=False,
    words=256,
    group=3,
    sets=2,
    sinks=None,
) -> list:
    """Queue ``c = silu(a @ b.T)``; returns `matmul`'s tile list. `sinks` holds
    one `words`-word scratch address per vector core (the in-place epilogue's
    stale first drain, `stream.send`)."""
    if (gm * gn) % words:
        raise ValueError(f"a {gm}x{gn} tile is not whole {words}-word RUNs")
    dims = ((1, SI.SLICE),)
    progs = stream.programs(
        SI.head(), lambda slots: SI.body(slots[0], words, group, sets), words, dims
    )
    cores = prog.units("VC")
    if sinks is None or len(sinks) < len(cores):
        raise ValueError(f"one sink a vector core: {len(cores)} needed")
    clusters = len(prog.units("MG"))
    ready, where, turn = [], [], 0
    events = list(
        MM.cluster_ops(
            prog, a_at, b_at, c_at, m, n, k, gm, gn, nk, stagger=stagger, late=late
        )
    )

    def epilogue(upto: int) -> None:
        nonlocal turn
        while ready and ready[0][0] <= upto:
            _, u, tok, tile = ready.pop(0)
            prog.wait(u, tok)
            at, span = tile[3], tile[1] * gn
            c = turn % len(cores)
            stream.send(
                prog,
                cores[c],
                progs,
                dims,
                words,
                at,
                at,
                span // words,
                words * 32,
                sink=sinks[c],
            )
            turn += 1

    for i, (u, ops, drained) in enumerate(events):
        prog.send(u, *ops)
        if drained:
            tok = prog.mark(u)
            ready.extend((i, u, tok, tile) for tile in drained)
            where += drained
        epilogue(i - clusters)
    epilogue(len(events))
    prog.barrier()
    return sorted(where, key=lambda t: t[3])
