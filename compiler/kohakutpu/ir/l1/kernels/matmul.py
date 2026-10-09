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
the bias in column 0. It accumulates, so it may emit.
"""

from itertools import pairwise

import numpy as np
from kohakutpu.hw import tensor as T
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


def ones_block(gm: int) -> bytes:
    """The A side of the bias K-block: column 0 is 1, `4*gm` rows."""
    ones = np.zeros((LANES * gm, KBLOCK), np.float16)
    ones[:, 0] = 1
    return pack(ones, gm, 1, 0)


def bias_block(bias, gn: int) -> bytes:
    """The B side of the bias K-block: column 0 is the bias, one row a channel."""
    block = np.zeros((len(bias), KBLOCK), np.float16)
    block[:, 0] = bias
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


def matmul(
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
) -> list:
    """Queue the program on `prog`; returns ``[(first sub-row, sub-rows, tile j,
    byte address)]``. `stagger` selects `plan`'s staggered bands.

    `parts` keeps fills (f), sweeps (s) and drains (d); dropping one measures the
    rest (a run without fills sweeps stale L1, without drains writes one sub-tile).
    """
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
            prog.send(
                u, *[Fill(ones_at, gm, sel=0, eoff=gm * nk, fbank=q) for q in (0, 1)]
            )
    tiles = plan(tm, tn, gm, len(mgs), stagger)
    held = {u: None for u in mgs}
    where = []
    out = c_at
    for step in range(max(len(t) for t in tiles)):
        for u, mine in zip(mgs, tiles, strict=True):
            if step >= len(mine):
                continue
            r0, h, j = mine[step]
            where.append((r0, h, j, out))
            ops = []
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
                    ops.append(held[u])
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
                    ops.append(held[u])
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
            if emit:
                held[u] = drain
            else:
                ops.append(drain)
            prog.send(u, *ops)
            out += h * gn * SUBTILE
    for u in mgs:
        if held[u] is not None:
            prog.send(u, held[u])
    prog.barrier()
    return where


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
