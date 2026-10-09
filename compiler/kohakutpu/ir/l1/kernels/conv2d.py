"""Hand-written L1 conv2d 3x3, stride 1, pad 1, channels-last.

No im2col and one quantise of the input. The image is cut into four horizontal
BANDS (each with a 1-row halo); an MXFP7 entry's four lanes are the same
(y, x) of the four bands, so every tap shift (dy, dx) is a WHOLE-entry shift.
Output positions are linearised over the padded width `Wp`, so a tap is the
constant offset ``dy*Wp + dx`` and a tile's A FILL is one contiguous run; the
two pad columns a row computes are discarded.

Channels go in CHUNKS of `cbc` 32-channel blocks, packed chunk-major, so a
tile's A fill for one (chunk, tap row) is one run of ``(gm + 2) * cbc``
entries that the three dx taps read at A offset ``dx * cbc``. A K-chunk is one
(channel chunk, tap); weights are packed in that order.
"""

import numpy as np
from kohakutpu.ir.l1.cluster import Drain, Gemm
from kohakutpu.ir.l1.kernels.matmul import BANK, ENTRY, LANES, SUBTILE, fills, pack
from kohakutpu.ir.l1.kernels.matmul import unpack as unpack_tiles
from kohakutpu.isa.cluster import ISA

TAPS = [(dy, dx) for dy in range(3) for dx in range(3)]


def geometry(h: int, w: int, gm: int) -> tuple[int, int, int]:
    """``(band rows, padded width, positions)`` -- positions rounded to tiles."""
    if h % LANES:
        raise ValueError(f"{h} rows do not split into {LANES} bands")
    hs, wp = h // LANES, w + 2
    return hs, wp, -(-hs * wp // gm) * gm


def chunk_blocks(cin: int, gm: int, gn: int) -> int:
    """The most 32-channel blocks a chunk can hold with both fills in one bank."""
    cb = cin // 32
    fits = [
        d
        for d in range(1, cb + 1)
        if cb % d == 0 and ISA.legal_nk(d) and (gm + 2) * d <= BANK and gn * d <= BANK
    ]
    best = max(fits, default=0)
    if not best:
        raise ValueError(f"tile {gm}x{gn} leaves no room for one channel block")
    return best


def _rows(h: int, w: int, gm: int) -> int:
    """Padded positions the input holds: every tap of the last tile exists."""
    hs, wp, positions = geometry(h, w, gm)
    return max(hs + 2, -(-(positions + 2 * wp + 2) // wp)) * wp


def pack_input(x, gm: int, cbc: int) -> bytes:
    """`(H, W, Cin)` fp16 as band-lane MXFP7 entries, ``(chunk, position, block)``."""
    h, w, c = x.shape
    hs, wp, _ = geometry(h, w, gm)
    xp = np.zeros((h + 2, wp, c), np.float16)
    xp[1 : h + 1, 1 : w + 1] = x
    out = np.zeros((_rows(h, w, gm) * LANES, c), np.float16)
    for k in range(LANES):
        band = xp[k * hs : k * hs + hs + 2]
        out[k : (hs + 2) * wp * LANES : LANES] = band.reshape(-1, c)
    step = 32 * cbc
    return b"".join(pack(out[:, s : s + step], 1, cbc, 0) for s in range(0, c, step))


def pack_weights(wt, gn: int, cbc: int) -> bytes:
    """`(Cout, Cin, 3, 3)` as B entries, K ordered (channel chunk, tap, channel)."""
    cout, cin = wt.shape[:2]
    step = 32 * cbc
    w = np.asarray(wt).transpose(0, 2, 3, 1)  # (cout, dy, dx, cin)
    parts = [w[..., s : s + step].reshape(cout, 9 * step) for s in range(0, cin, step)]
    return pack(np.concatenate(parts, axis=1), gn, cbc, 1)


def conv(prog, a_at, b_at, c_at, h, w, cin, cout, gm, gn, cbc=None) -> list:
    """Queue the program; returns ``[(first sub-row, sub-rows, tile j, byte address)]``."""
    if cin % 32 or cout % (LANES * gn):
        raise ValueError(f"{cin}->{cout} does not tile as {gm}x{gn}")
    cbc = cbc or chunk_blocks(cin, gm, gn)
    if (gm + 2) * cbc > BANK or gn * cbc > BANK or (cin // 32) % cbc:
        raise ValueError(f"{cbc} blocks a chunk do not fit tile {gm}x{gn}")
    chunks = cin // (32 * cbc)
    _, wp, positions = geometry(h, w, gm)
    tm, tn = positions // gm, cout // (LANES * gn)
    a_chunk = _rows(h, w, gm) * cbc * ENTRY
    b_tile = gn * len(TAPS) * chunks * cbc * ENTRY
    order = [(kc, dy, dx) for kc in range(chunks) for dy, dx in TAPS]
    mgs = prog.units("MG")
    bank = {u: 0 for u in mgs}
    abank = {u: 0 for u in mgs}
    held = {u: None for u in mgs}
    where = []
    for t in range(tm * tn):
        i, j = divmod(t, tn)
        u = mgs[t % len(mgs)]
        out = c_at + t * gm * gn * SUBTILE
        where.append((i * gm, gm, j, out))
        ops = []
        emit = False
        for c, (kc, dy, dx) in enumerate(order):
            q = bank[u] % 2
            bank[u] += 1
            if dx == 0:
                abank[u] += 1
                first = a_at + kc * a_chunk + (i * gm + dy * wp) * cbc * ENTRY
                ops += fills(first, (gm + 2) * cbc, 0, abank[u] % 2)
            ops += fills(b_at + j * b_tile + c * gn * cbc * ENTRY, gn * cbc, 1, q)
            last = c == len(order) - 1
            emit = last and ISA.can_emit(int(c > 0), cbc)
            if last and held[u] is not None:
                ops.append(held[u])
                held[u] = None
            ops.append(
                Gemm(
                    gm,
                    gn,
                    cbc,
                    acc=c > 0,
                    aoff=dx * cbc,
                    abank=abank[u] % 2,
                    bbank=q,
                    emit=emit,
                    addr=out,
                )
            )
        drain = Drain(out, gm * gn, fuse=emit)
        if emit:
            held[u] = drain
        else:
            ops.append(drain)
        prog.send(u, *ops)
    for u in mgs:
        if held[u] is not None:
            prog.send(u, held[u])
    prog.barrier()
    return where


def unpack(get, where, h, w, cout, gm, gn) -> np.ndarray:
    """The `(H, W, Cout)` result from the drained tiles."""
    hs, wp, positions = geometry(h, w, gm)
    y = unpack_tiles(get, where, positions * LANES, cout, gm, gn)
    out = np.zeros((h, w, cout))
    for k in range(LANES):
        band = y[k::LANES][: hs * wp].reshape(hs, wp, cout)
        out[k * hs : (k + 1) * hs] = band[:, :w]
    return out
