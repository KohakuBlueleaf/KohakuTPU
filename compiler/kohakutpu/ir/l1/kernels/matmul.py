"""Hand-written L1 matmul `c = a @ b.T` and linear+bias, on every cluster.

Operands are MXFP7 entries packed TILE-MAJOR (`pack_a`/`pack_b`), so every
(tile, K-chunk) FILL is one contiguous run (two where a staggered row tile
crosses a packed tile). `plan` deals output tiles to the clusters; a cluster's
L1 bank alternates per K-chunk ACROSS tiles, so a FILL never
lands on the bank being swept. A tile's last sweep emits and its fused DRAIN is
placed LATE -- after the next tile's non-emitting work -- because a fused DRAIN
holds the sequencer until its last sub-tile has left.

Bias: one more ACCUMULATING GEMM per tile over a K-block whose A is a constant
ones block (resident in both banks after the chunk entries) and whose B holds
the bias split over two columns (`bias_block`). It accumulates, so it may emit.
"""

from itertools import pairwise

import numpy as np
from kohakutpu.hw import tensor as T
from kohakutpu.hw.mxfp7 import value_fp16
from kohakutpu.ir.l1.cluster import Drain, Fill, Gemm
from kohakutpu.isa.cluster import ISA

ENTRY, SUBTILE, LANES, KBLOCK = 128, 32, 4, 32
#: Entries one L1 bank holds a side.
BANK = 256


def pack(x, groups: int, blocks: int, blayout: int) -> bytes:
    """A `(rows, K)` fp16 operand as MXFP7 entries, tile-major."""
    words = T.to_mxfp7_words_tiled(np.asarray(x, np.float16), groups, blocks, blayout)
    return b"".join(w.to_bytes(32, "little") for w in words)


def pack_a(x, gm: int, nk: int) -> bytes:
    return pack(x, gm, nk, 0)


def pack_b(w, gn: int, nk: int) -> bytes:
    return pack(w, gn, nk, 1)


#: The bias K-block's A row: two columns, so the bias rides as hi*A0 + lo*A1.
#: One column carries it at MXFP7's int7 (rel_l2 4.7e-3, and A0 quantises to
#: 1.00195, not 1); two carry it to rel_l2 8.3e-5, past fp16, in the same
#: sweep (MEASURED, host model of mx_quant.v). 2**-12 for a third underflows.
BIAS_A = (1.0, 2.0**-6)


def _ones(rows: int):
    ones = np.zeros((rows, KBLOCK), np.float16)
    for c, v in enumerate(BIAS_A):
        ones[:, c] = v
    return ones


#: The A row's values as the clusters read them, after quantisation.
BIAS_AQ = value_fp16(_ones(1))[0, : len(BIAS_A)]


def ones_block(gm: int) -> bytes:
    """The A side of the bias K-block: `BIAS_A` in every one of `4*gm` rows."""
    return pack(_ones(LANES * gm), gm, 1, 0)


def bias_block(bias, gn: int) -> bytes:
    """The B side of the bias K-block, one row a channel: column `c` holds what
    the bias still lacks over `BIAS_AQ[c]`, so the row's dot product with the
    ones row is the bias to past fp16 precision."""
    v = np.asarray(bias, np.float64)
    block = np.zeros((len(v), KBLOCK), np.float16)
    have = np.zeros(len(v))
    for c, a in enumerate(BIAS_AQ):
        block[:, c] = ((v - have) / a).astype(np.float16)
        have = value_fp16(block)[:, : c + 1] @ BIAS_AQ[: c + 1]
    return pack(block, gn, 1, 1)


def fills(addr: int, n: int, sel: int, bank: int, eoff: int = 0) -> list:
    """`n` consecutive entries into one bank from entry `eoff`: a FILL counts at most 255."""
    if n > BANK:
        raise ValueError(f"{n} entries pass a {BANK}-entry bank")
    if n <= 255:
        return [Fill(addr, n, sel=sel, eoff=eoff, fbank=bank)]
    half = n // 2
    return [
        Fill(addr, half, sel=sel, eoff=eoff, fbank=bank),
        Fill(addr + half * ENTRY, n - half, sel=sel, eoff=eoff + half, fbank=bank),
    ]


def plan(tm: int, tn: int, gm: int, clusters: int, stagger: bool) -> list[list]:
    """Each cluster's output tiles in order, as ``(first sub-row, sub-rows, j)``.

    Round robin hands tile `t` to cluster ``t % clusters``: equal tiles end
    together, so every cluster drains at once. `stagger` gives cluster `c` the
    column tiles ``j % clusters == c`` and starts its first row tile
    ``c / clusters`` of a tile short, so the clusters' drains fall a quarter
    tile apart (MEASURED: one cluster drains 18.8 B/cycle, four at once 21.7).
    """
    if not stagger:
        out: list[list] = [[] for _ in range(clusters)]
        for t in range(tm * tn):
            i, j = divmod(t, tn)
            out[t % clusters].append((i * gm, gm, j))
        return out
    if tn % clusters:
        raise ValueError(f"{tn} column tiles do not split over {clusters} clusters")
    rows = tm * gm
    out = []
    for c in range(clusters):
        first = gm - c * gm // clusters
        cuts = [0] + list(range(first, rows, gm)) + [rows]
        spans = [(lo, hi - lo) for lo, hi in pairwise(cuts) if hi > lo]
        out.append([(r0, h, j) for j in range(c, tn, clusters) for r0, h in spans])
    return out


def a_runs(a_at, r0, h, c, gm, nk, chunks) -> list:
    """``(address, entries, L1 offset)`` runs holding sub-rows ``[r0, r0+h)`` of
    K-chunk `c`: one per packed tile the span crosses."""
    out = []
    for t in range(r0 // gm, (r0 + h - 1) // gm + 1):
        lo, hi = max(r0, t * gm), min(r0 + h, (t + 1) * gm)
        first = (t * chunks + c) * gm * nk + (lo - t * gm) * nk
        out.append((a_at + first * ENTRY, (hi - lo) * nk, (lo - r0) * nk))
    return out


def cluster_ops(
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
    parts="fsd",
    ones_at=None,
    bias_at=None,
    stagger=False,
    late=True,
):
    """The clusters' words, tile by tile: yields ``(unit, ops, drained)`` in send
    order, `drained` the ``(first sub-row, sub-rows, tile j, byte address)``
    tiles whose DRAIN is in `ops`. `late` places a fused DRAIN after the next
    tile's non-emitting work (it holds the sequencer until its last sub-tile has
    left); otherwise right behind its tile, so the tile is in memory sooner."""
    if m % (LANES * gm) or n % (LANES * gn) or k % (KBLOCK * nk):
        raise ValueError(f"{m}x{k}x{n} is not whole {gm}x{gn}x{nk} tiles")
    tm, tn, chunks = m // (LANES * gm), n // (LANES * gn), k // (KBLOCK * nk)
    b_tile = gn * chunks * nk * ENTRY
    mgs = prog.units("MG")
    bank = {u: 0 for u in mgs}
    biased = bias_at is not None
    if biased:
        if gm * nk + gm > BANK or gn * nk + gn > BANK:
            raise ValueError("the bias block does not fit beside a chunk in one bank")
        for u in mgs:
            ones = [Fill(ones_at, gm, sel=0, eoff=gm * nk, fbank=q) for q in (0, 1)]
            yield u, ones, []
    tiles = plan(tm, tn, gm, len(mgs), stagger)
    held = {u: None for u in mgs}
    out = c_at
    for step in range(max(len(t) for t in tiles)):
        for u, mine in zip(mgs, tiles, strict=True):
            if step >= len(mine):
                continue
            r0, h, j = mine[step]
            tile = (r0, h, j, out)
            ops, drained = [], []
            emit = False
            for c in range(chunks):
                q = bank[u] % 2
                bank[u] += 1
                if "f" in parts:
                    for at, cnt, eoff in a_runs(a_at, r0, h, c, gm, nk, chunks):
                        ops += fills(at, cnt, 0, q, eoff)
                    ops += fills(b_at + j * b_tile + c * gn * nk * ENTRY, gn * nk, 1, q)
                last = c == chunks - 1 and not biased
                emit = last and "d" in parts and ISA.can_emit(int(c > 0), nk)
                if last and held[u] is not None:
                    ops.append(held[u][0])
                    drained.append(held[u][1])
                    held[u] = None
                if "s" in parts:
                    ops.append(
                        Gemm(
                            h, gn, nk, acc=c > 0, abank=q, bbank=q, emit=emit, addr=out
                        )
                    )
            if biased:
                q = bank[u] % 2
                bank[u] += 1
                ops.append(
                    Fill(bias_at + j * gn * ENTRY, gn, sel=1, eoff=gn * nk, fbank=q)
                )
                emit = "d" in parts
                if held[u] is not None:
                    ops.append(held[u][0])
                    drained.append(held[u][1])
                    held[u] = None
                ops.append(
                    Gemm(
                        h,
                        gn,
                        1,
                        acc=True,
                        aoff=gm * nk,
                        boff=gn * nk,
                        abank=q,
                        bbank=q,
                        emit=emit,
                        addr=out,
                    )
                )
            drain = Drain(out, h * gn if "d" in parts else 1, fuse=emit)
            if emit and late:
                held[u] = (drain, tile)
            else:
                ops.append(drain)
                drained.append(tile)
            yield u, ops, drained
            out += h * gn * SUBTILE
    for u in mgs:
        if held[u] is not None:
            yield u, [held[u][0]], [held[u][1]]


def matmul(prog, a_at, b_at, c_at, m, n, k, gm, gn, nk, parts="fsd", **kw) -> list:
    """Queue the program on `prog`; returns ``[(first sub-row, sub-rows, tile j,
    byte address)]``. `kw` is `cluster_ops`'s (bias, `stagger`, `late`).

    `parts` keeps fills (f), sweeps (s) and drains (d); dropping one measures the
    rest (a run without fills sweeps stale L1, without drains writes one sub-tile).
    """
    where = []
    for u, ops, drained in cluster_ops(
        prog, a_at, b_at, c_at, m, n, k, gm, gn, nk, parts, **kw
    ):
        prog.send(u, *ops)
        where += drained
    prog.barrier()
    return sorted(where, key=lambda t: t[3])


def unpack(get, where, m, n, gm, gn) -> np.ndarray:
    """The `(m, n)` result from the drained tiles; `get(addr, nbytes)` reads memory."""
    c = np.zeros((m, n))
    for r0, h, j, at in where:
        raw = get(at, h * gn * SUBTILE)
        words = [
            int.from_bytes(raw[s : s + 32], "little") for s in range(0, len(raw), 32)
        ]
        c[r0 * 4 : (r0 + h) * 4, j * 4 * gn : (j + 1) * 4 * gn] = T.unpack_c(
            words, 4 * h, 4 * gn, gn
        )
    return c
