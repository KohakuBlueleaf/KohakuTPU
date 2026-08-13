"""The byte orders a KohakuTPU array can be in.

Level 2. An operand packed for the wrong tiling is the right bytes in the wrong
places, and the machine cannot tell.

* :class:`Entry` -- L1 entries of `lanes x kblock`, tile-major. What FILL
  streams, sized by the kernel's `gm`/`gn`/`nk`.
* :class:`ConvEntry` -- an activation as `[C/32][plane][32]`, a 3x3 tap being a
  constant offset rather than a stride. What a convolution FILLs.
* :class:`Tile` -- one 32-byte word per 4x4 sub-tile, blocked per instance.
* :class:`ChannelBias` -- an `(N,)` vector as sub-tile words, each column group
  repeated down its rows. Read at stride 0 beside a fused epilogue's tile.
* :class:`Flat` -- row-major FP16. The only order whose rows are rows.
"""

from dataclasses import dataclass

import numpy as np
from kohakutpu.hw import tensor as T

WORD_BYTES = 32
LANES = T.LANES
KBLOCK = T.KBLOCK


class LayoutError(ValueError):
    """An array that cannot be put into this order, and why."""


@dataclass(frozen=True)
class Entry:
    """An operand image, tile-major in `groups x blocks` entries.

    `groups` is lane-groups per tile and `blocks` is K-blocks per chunk, so one
    FILL streams one contiguous run. Padding is to WHOLE TILES.
    """

    groups: int
    blocks: int

    @property
    def key(self) -> str:
        return f"entry:{self.groups}x{self.blocks}"

    def padded(self, shape: tuple) -> tuple:
        rows, k = shape
        lane = self.groups * LANES
        kb = self.blocks * KBLOCK
        return (-(-rows // lane) * lane, -(-k // kb) * kb)

    def nbytes(self, shape: tuple) -> int:
        rows, k = self.padded(shape)
        return rows * k * 2

    def pack(self, array) -> bytes:
        arr = np.asarray(array, np.float16)
        if arr.ndim != 2:
            raise LayoutError(f"an operand is 2-d; got {arr.shape}")
        rows, k = self.padded(arr.shape)
        out = np.zeros((rows, k), np.float16)
        out[: arr.shape[0], : arr.shape[1]] = arr
        words = T.to_fp16_words_tiled(out, self.groups, self.blocks)
        return b"".join(w.to_bytes(WORD_BYTES, "little") for w in words)

    def unpack(self, raw: bytes, shape: tuple):
        rows, k = self.padded(shape)
        flat = np.frombuffer(raw, np.float16)[: rows * k]
        e = flat.reshape(
            rows // (self.groups * LANES),
            k // (self.blocks * KBLOCK),
            self.groups,
            self.blocks,
            LANES,
            KBLOCK,
        )
        e = e.transpose(0, 2, 4, 1, 3, 5).reshape(rows, k)
        return np.asarray(e[: shape[0], : shape[1]], np.float64)


@dataclass(frozen=True)
class ChannelBias:
    """A per-channel vector as the sub-tile words a fused epilogue reads.

    One 4x4 sub-tile is one 256-bit word, so column group `c` wants
    `bias[4c:4c+4]` repeated down the sub-tile's four rows. A VLD walks WORDS
    (`model.py:726`), so `gn` of these cover a tile's columns and a `gm`-deep
    tile re-reads them at stride 0.

    `4n` elements, against the `M*n` a materialised broadcast would cost.
    """

    gn: int

    @property
    def key(self) -> str:
        return f"bias:{self.gn}"

    def padded(self, shape: tuple) -> tuple:
        wide = self.gn * LANES
        return (-(-shape[0] // wide) * wide,)

    def nbytes(self, shape: tuple) -> int:
        return self.padded(shape)[0] * LANES * 2

    def pack(self, array) -> bytes:
        arr = np.asarray(array, np.float16).reshape(-1)
        out = np.zeros(self.padded(arr.shape)[0], np.float16)
        out[: arr.size] = arr
        return np.repeat(out.reshape(-1, LANES)[:, None, :], LANES, axis=1).tobytes()

    def unpack(self, raw: bytes, shape: tuple):
        got = np.frombuffer(raw, np.float16).reshape(-1, LANES, LANES)
        return np.asarray(got[:, 0, :].reshape(-1)[: shape[0]], np.float64)


@dataclass(frozen=True)
class ConvEntry:
    """An activation as `[C/32][plane][32]`: the 32-channel block outermost.

    Four adjacent pixels of one block are 256 contiguous bytes -- one L1 entry --
    so a fill is one run at `nk = 1` and a 3x3 tap is a constant added to the
    address. `Entry` cannot describe this: its runs tile the operand, and nine
    taps are nine OVERLAPPING windows at 64-byte offsets.

    `gm` is the tile height the sweep will use, which decides only the tail --
    the last tile reads a tap past the plane's end.
    """

    pad: int = 1
    gm: int = 8

    @property
    def key(self) -> str:
        return f"conv:{self.pad}:{self.gm}"

    def geometry(self, shape: tuple) -> tuple:
        """``(wp, plane, tail)`` for an ``[H][W][C]`` activation, in positions."""
        h, w, _ = shape
        _, wp, plane = T.conv_geometry(h, w, self.pad)
        useful = (h - 1) * wp + w
        swept = -(-useful // (self.gm * LANES)) * self.gm * LANES
        return wp, plane, max(0, -(-(swept + 2 * wp + 2 - plane) // LANES))

    def nbytes(self, shape: tuple) -> int:
        _, plane, tail = self.geometry(shape)
        blocks = -(-shape[2] // KBLOCK)
        return (blocks * plane + tail * LANES) * KBLOCK * 2

    def pack(self, array) -> bytes:
        arr = np.asarray(array, np.float16)
        if arr.ndim != 3:
            raise LayoutError(f"a conv activation is [H][W][C]; got {arr.shape}")
        _, _, tail = self.geometry(arr.shape)
        words = T.to_fp16_words_conv(arr, self.pad, tail)
        return b"".join(w.to_bytes(WORD_BYTES, "little") for w in words)

    def unpack(self, raw: bytes, shape: tuple):
        words = [
            int.from_bytes(raw[at : at + WORD_BYTES], "little")
            for at in range(0, len(raw), WORD_BYTES)
        ]
        return np.asarray(
            T.from_fp16_words_conv(words, tuple(shape), self.pad), np.float64
        )


@dataclass(frozen=True)
class Tile:
    """A drained result: 4x4 sub-tiles, blocked by grid instance.

    Each instance writes ITS OWN `gm x gn` sub-tiles contiguously, so the region
    is instance-blocked rather than row-major: the image is
    ``(gi, gj, gm, gn, 4, 4)`` against a matrix of ``(gi, gm, 4, gj, gn, 4)``.
    That makes both directions one transpose, and a sub-tile at a time a
    2,560-iteration Python loop for the same bytes.
    """

    grid: tuple
    gm: int
    gn: int

    @property
    def key(self) -> str:
        return f"tile:{self.grid[0]}x{self.grid[1]}:{self.gm}x{self.gn}"

    @property
    def span(self) -> int:
        """Sub-tiles one instance drains."""
        return self.gm * self.gn

    def nbytes(self, shape: tuple) -> int:
        return self.grid[0] * self.grid[1] * self.span * WORD_BYTES

    def pack(self, array) -> bytes:
        arr = np.asarray(array, np.float16)
        gi, gj = self.grid
        padded = np.zeros(self._padded(), np.float16)
        padded[: arr.shape[0], : arr.shape[1]] = arr
        held = padded.reshape(gi, self.gm, LANES, gj, self.gn, LANES)
        return held.transpose(0, 3, 1, 4, 2, 5).reshape(-1).tobytes()

    def unpack(self, raw: bytes, shape: tuple):
        gi, gj = self.grid
        flat = np.frombuffer(raw, np.float16).astype(np.float64)
        want = gi * gj * self.span * LANES * LANES
        # A short buffer leaves the sub-tiles it does not reach at zero, which
        # is what reading back a partly drained region has always given.
        if flat.size < want:
            flat = np.concatenate([flat, np.zeros(want - flat.size)])
        image = flat[:want].reshape(gi, gj, self.gm, self.gn, LANES, LANES)
        full = image.transpose(0, 2, 4, 1, 3, 5).reshape(self._padded())
        m, n = shape
        out = np.zeros((m, n))
        rows, cols = min(m, full.shape[0]), min(n, full.shape[1])
        out[:rows, :cols] = full[:rows, :cols]
        return out

    def _padded(self) -> tuple:
        """The whole-sub-tile shape this layout covers, as ``(rows, cols)``."""
        return (self.grid[0] * self.gm * LANES, self.grid[1] * self.gn * LANES)


@dataclass(frozen=True)
class Flat:
    """Row-major FP16. The only order whose rows are rows.

    A reduction along a row needs this; elementwise work does not.
    """

    @property
    def key(self) -> str:
        return "flat"

    def nbytes(self, shape: tuple) -> int:
        return int(np.prod(shape)) * 2

    def pack(self, array) -> bytes:
        return np.ascontiguousarray(array, np.float16).tobytes()

    def unpack(self, raw: bytes, shape: tuple):
        n = int(np.prod(shape))
        return np.frombuffer(raw, np.float16)[:n].reshape(shape).astype(np.float64)
