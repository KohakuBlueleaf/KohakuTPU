"""Operand layout: MXFP7 packing for the clusters and the drained sub-tile form.

THE LAYOUT CONTRACT. Every operand tensor is stored row-major as FP16:

    [lanes][K]      lanes % 4 == 0,  K % 32 == 0

    A operand   lanes = M rows of A
    B operand   lanes = N columns of B, i.e. B stored TRANSPOSED
    C output    [M][N] row-major

The hardware assumes this and never checks it; zero padding is numerically
inert (a zero contributes nothing to a dot product, and the block scale ignores
it). ``A @ B.T`` is ``torch.nn.Linear``'s product with its ``(out, in)`` weight
as stored, and ``C[M][N]`` is already the next layer's A operand.
"""

import numpy as np
from kohakutpu.language.numerics import mxfp7

LANES = 4
KBLOCK = 32
#: One MXFP7 L1 entry: 4 lanes x 32 int7 and 4 E5M3 scales, as four 256-bit
#: words.
WORDS = 4


def pack(x, groups: int, blocks: int, blayout: int) -> bytes:
    """A `(rows, K)` fp16 operand as MXFP7 entries, tile-major.

    Entries run (tile, K-chunk, group, block): tiles of `groups` lane groups,
    chunks of `blocks` K-blocks, so one pass's entries are one run a FILL
    streams. Word `w` of an entry carries K ``[w*8, w*8+8)`` as 32 int7 at
    slot ``lane*8 + kk`` (B packing, `blayout`: ``kk*4 + lane``), MSB-first
    from bit 255, and all four E5M3 scales in its low 32 bits, lane 0 highest
    (`mx_quant.v`).

    Raises :class:`ValueError` unless the operand is padded to whole tiles.
    """
    x = np.asarray(x, np.float16)
    lanes, k = x.shape
    gt, bc = groups, blocks
    g_total, nk_total = lanes // LANES, k // KBLOCK
    if lanes % LANES or k % KBLOCK or g_total % gt or nk_total % bc:
        raise ValueError("pad the operand to whole tiles before packing")

    q, es, m8 = mxfp7.quantise_fp16(x)
    q = np.asarray(q, np.int64) & 0x7F
    field = (((np.asarray(es) + mxfp7.SBIAS) & 0x1F) << 3) | ((np.asarray(m8) - 8) & 7)

    # (group, lane, block, k) -> entries in (tile, chunk, group, block) order.
    q = q.reshape(g_total // gt, gt, LANES, nk_total // bc, bc, KBLOCK)
    q = q.transpose(0, 3, 1, 4, 2, 5).reshape(-1, LANES, WORDS, 8)
    field = field.reshape(g_total // gt, gt, LANES, nk_total // bc, bc)
    field = field.transpose(0, 3, 1, 4, 2).reshape(-1, LANES)

    # slots: (entry, word, lane, kk), lane-major for A and kk-major for B.
    slots = q.transpose(0, 2, 1, 3)
    if blayout:
        slots = slots.transpose(0, 1, 3, 2)
    slots = slots.reshape(len(q), WORDS, 32)
    shift7 = np.arange(6, -1, -1)
    shift8 = np.arange(7, -1, -1)
    data = ((slots[..., None] >> shift7) & 1).reshape(len(q), WORDS, 224)
    scale = ((field[..., None] >> shift8) & 1).reshape(len(q), 1, 32)
    bits = np.concatenate([data, np.broadcast_to(scale, (len(q), WORDS, 32))], -1)
    # MSB-first bits are a big-endian word; memory holds it little-endian.
    return np.packbits(bits.astype(np.uint8), axis=-1)[..., ::-1].tobytes()


def pack_a(x, gm: int, nk: int) -> bytes:
    """The clusters' A packing: tiles of `gm` lane groups, `nk` K-blocks a chunk."""
    return pack(x, gm, nk, 0)


def pack_b(w, gn: int, nk: int) -> bytes:
    """The clusters' B packing: tiles of `gn` lane groups, `nk` K-blocks a chunk."""
    return pack(w, gn, nk, 1)


def fp16_values(bits) -> np.ndarray:
    """FP16 bit patterns as float64, the accumulator's way: no subnormals (an
    exponent of 0 is zero) and no infinities (31 is an ordinary exponent)."""
    b = np.asarray(bits, np.int64)
    exp = (b >> 10) & 0x1F
    v = (1.0 + (b & 0x3FF) / 1024.0) * np.exp2(exp - 15.0)
    v = np.where(exp == 0, 0.0, v)
    return np.where(b & 0x8000, -v, v)


def unpack_tiles(raw: bytes, m: int, n: int, gm: int, gn: int) -> np.ndarray:
    """A drained ``[m][n]`` from tiles of ``gm x gn`` 4x4 sub-tiles: tiles
    row-major, sub-tiles row-major in a tile, element (i, j) of a sub-tile at
    fp16 ``4*i + j`` of its 32-byte word."""
    h = np.frombuffer(raw, "<u2")[: m * n]
    h = h.reshape(m // (4 * gm), n // (4 * gn), gm, gn, 4, 4)
    return fp16_values(h.transpose(0, 2, 4, 1, 3, 5).reshape(m, n))


__all__ = ["KBLOCK", "LANES", "fp16_values", "pack", "pack_a", "pack_b", "unpack_tiles"]
