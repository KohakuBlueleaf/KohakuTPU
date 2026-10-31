"""An exact on-card transpose of f16 ``[nb, A, B]`` to ``[nb, B, A]`` (A and B
multiples of 16): elements move inside a 32-byte word, which no mover walk
does, so a word's 4 x 4 element blocks are transposed in the vector core's
lanes and the mover moves everything else.

1. mover GT4: four rows of a 16-column word to four 4 x 4 sub-tiles (one a
   word, element (i, j) at lane 4i + j);
2. vector core: each sub-tile transposed in its chunk -- lane 4i + j takes
   lane 4j + i: two bit swaps, each a crossbar xor (5, then 10) under a lane
   mask (`kt.xlane` + `kt.where`, exact);
3. mover GT4 again: the sub-tiles of each output row group, gathered by the
   source walk, back to four rows of a 16-column word;
4. mover copy: those groups to row-major ``[nb, B, A]``.
"""

import numpy as np

from kohakutpu.application.kernels import vector as V
from kohakutpu.language import dsl as kt

#: Rows of 128 values (8 sub-tiles) a vector-core trip transforms.
ROWS = 8
_MASKS: np.ndarray | None = None


def masks() -> np.ndarray:
    """[2, 128] f16: lanes whose bits 0 and 2 differ, then bits 1 and 3."""
    global _MASKS
    if _MASKS is None:
        lane = np.arange(128) % 16
        m1 = ((lane >> 0) & 1) != ((lane >> 2) & 1)
        m2 = ((lane >> 1) & 1) != ((lane >> 3) & 1)
        _MASKS = np.stack([m1, m2]).astype(np.float16)
        _MASKS.setflags(write=False)
    return _MASKS


def emit(
    x,
    mask,
    m0: int,
    y,
    nb: int,
    A: int,
    B: int,
    cores: int,
    *,
    shape_class=None,
    scratch_space="dram",
    scratch_node=None,
):
    """The transpose's statements in the kernel being traced: ``x`` (f16
    [nb, A, B] as words) into ``y`` ([nb, B, A]), the lane masks at rows
    `m0`, `m0` + 1 of ``mask`` (`masks()`). Scratch may live in a named node's
    staging store so that this node's mover writes locally."""
    words = nb * A * B // 16
    lines = words // 8
    P, rg, cw = 2 * B, A // 4, B // 16
    sub = B // 4  # sub-tiles in a row group of x
    trips = -(-lines // ROWS)
    ncore = cores if shape_class and shape_class.cores == "all" else min(cores, trips)
    padded = -(-trips // ncore) * ncore * ROWS if shape_class else trips * ROWS
    placement = {"space": scratch_space}
    if scratch_node is not None:
        placement["node"] = scratch_node
    s = kt.buffer((padded, 128), "f16", **placement)
    t = kt.buffer((padded, 128), "f16", **placement)
    z = kt.buffer((words, 16), "f16", **placement)
    if shape_class:
        seed = kt.buffer((16,), "f16", **placement)
        kt.fill_value(seed, 0)
        kt.copy(
            s,
            seed,
            walk=((padded, 0), (8, 0)),
            dwalk=((padded, 256), (8, 32)),
            classed=True,
            node_loop=True,
        )
    kt.gt4(
        s,
        x,
        walk=[(nb, A * P), (rg, 4 * P), (cw, 32), (4, P)],
        dwalk=[(nb, rg * cw * 128), (rg, cw * 128), (cw, 128)],
        node_loop=shape_class is not None,
    )
    used, start, n = V.split(trips, cores)
    if shape_class:
        used = ncore
    for c in kt.par(used, on=kt.vc, classed=shape_class is not None):
        m1 = kt.unpack(kt.load(mask[m0 : m0 + 1, :]))
        m2 = kt.unpack(kt.load(mask[m0 + 1 : m0 + 2, :]))
        for k in kt.range((trips + used - 1 - c) // used if shape_class else n(c)):
            r = (c + k * used) * ROWS if shape_class else (start(c) + k) * ROWS
            v = kt.unpack(kt.load(s[r : r + ROWS, :]))
            a = kt.where(m1, kt.xlane(v, 5), v)
            kt.store(t[r : r + ROWS, :], kt.where(m2, kt.xlane(a, 10), a))
    # Output row group j (columns 4j.. of x), output word w: the sub-tiles
    # (row group 4w + q, column group j) of x, q = 0..3.
    kt.gt4(
        z,
        t,
        walk=[
            (nb, rg * sub * 32),
            (sub, 32),
            (A // 16, 4 * sub * 32),
            (4, sub * 32),
        ],
        dwalk=[(nb, sub * (A // 16) * 128), (sub, (A // 16) * 128), (A // 16, 128)],
        node_loop=shape_class is not None,
    )
    kt.copy(
        y,
        z,
        walk=[
            (nb, sub * (A // 16) * 128),
            (sub, (A // 16) * 128),
            (4, 32),
            (A // 16, 128),
        ],
        dwalk=[(words, 32)],
        classed=shape_class is not None,
        node_loop=2 if shape_class else False,
    )


__all__ = ["ROWS", "emit", "masks"]
