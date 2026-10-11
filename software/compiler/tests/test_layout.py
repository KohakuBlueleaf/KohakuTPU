"""Operand layouts against their definitions written out element by element:
the MXFP7 entry bits slot by slot, a drained tile sub-tile by sub-tile."""

import numpy as np
import pytest
from kohakutpu.compiler.layout import tensor as T
from kohakutpu.compiler.layout.buffer import Buffer
from kohakutpu.language.numerics import mxfp7
from kohakutpu.language.numerics.mxfp7 import fp16_to_float
from kohakutpu.language.text.syntax import parse


def pack_by_slot(x, gt, bc, blayout) -> bytes:
    """`T.pack` from its definition: each entry, word, slot and scale placed."""
    q, es, m8 = mxfp7.quantise_fp16(x)
    q = np.asarray(q, np.int64) & 0x7F
    field = (((np.asarray(es) + mxfp7.SBIAS) & 0x1F) << 3) | ((np.asarray(m8) - 8) & 7)
    lanes, k = x.shape
    out = []
    for t in range(lanes // 4 // gt):
        for c in range(k // 32 // bc):
            for g in range(gt):
                for b in range(bc):
                    lane0, blk = (t * gt + g) * 4, c * bc + b
                    for w in range(4):
                        acc = 0
                        for oi in range(32):
                            lane, kk = (
                                (oi % 4, oi // 4) if blayout else (oi // 8, oi % 8)
                            )
                            acc |= int(q[lane0 + lane, blk * 32 + w * 8 + kk]) << (
                                249 - oi * 7
                            )
                        for lane in range(4):
                            acc |= int(field[lane0 + lane, blk]) << (24 - lane * 8)
                        out.append(acc.to_bytes(32, "little"))
    return b"".join(out)


@pytest.mark.parametrize(
    "rows, k, gt, bc", [(8, 64, 1, 1), (16, 128, 2, 2), (32, 64, 4, 1)]
)
@pytest.mark.parametrize("blayout", [0, 1])
def test_pack_places_every_slot_and_scale(rows, k, gt, bc, blayout):
    rng = np.random.default_rng(rows + k + blayout)
    x = (rng.standard_normal((rows, k)) * 3).astype(np.float16)
    x[0, :32] = 0  # an all-zero block: the zero scale
    assert T.pack(x, gt, bc, blayout) == pack_by_slot(x, gt, bc, blayout)


def test_pack_refuses_a_partial_tile():
    with pytest.raises(ValueError):
        T.pack(np.zeros((12, 64), np.float16), 2, 1, 0)


def test_a_drained_tile_reads_sub_tile_by_sub_tile():
    m, n, gm, gn = 16, 32, 2, 4
    rng = np.random.default_rng(0)
    bits = rng.integers(0, 1 << 16, size=(m * n,), dtype=np.uint16)
    bits[:8] = [0x0000, 0x8000, 0x0001, 0x83FF, 0x7C00, 0xFC00, 0x7FFF, 0x3C00]
    raw = bits.tobytes()
    want = np.zeros((m, n))
    per_tile = gm * gn
    for word in range(m * n // 16):
        tile, sub = divmod(word, per_tile)
        ti, tj = divmod(tile, n // (4 * gn))
        si, sj = divmod(sub, gn)
        for e in range(16):
            i, j = divmod(e, 4)
            row = 4 * gm * ti + 4 * si + i
            col = 4 * gn * tj + 4 * sj + j
            want[row, col] = fp16_to_float(int(bits[16 * word + e]))
    assert np.array_equal(T.unpack_tiles(raw, m, n, gm, gn), want)


def test_a_tiled_buffer_reads_back_what_it_wrote():
    (st,) = parse("b = buffer : f16[16, 32] @dram(tile=2x2)")
    b = Buffer.of(st.annot)
    x = np.random.default_rng(1).standard_normal((16, 32)).astype(np.float16)
    assert np.array_equal(b.unpack(b.pack(x)), x.astype(np.float64))
