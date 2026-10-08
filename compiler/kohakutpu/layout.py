"""The byte orders a KohakuTPU array can be in.

Level 2. An operand packed for the wrong tiling is the right bytes in the wrong
places, and the machine cannot tell.

* :class:`Entry` -- L1 entries of `lanes x kblock`, tile-major. What FILL
  streams, sized by the kernel's `gm`/`gn`/`nk`.
* :class:`PadHWC` -- an activation as a zero-padded channels-last image, what a
  convolution's on-card im2col (the mover) reads.
* :class:`MxEntry` -- `Entry` pre-quantised to MXFP7: what a cluster FILLs.
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
class MxEntry:
    """An operand as the cluster FILLs it: `Entry`'s order, each entry 128-byte MXFP7.

    `blayout` is the B side's transposed slot map; read back dequantised.
    """

    groups: int
    blocks: int
    blayout: int = 0
    entry_bytes = T.MXFP7_ENTRY_BYTES

    @property
    def key(self) -> str:
        return f"mxentry:{self.groups}x{self.blocks}:{self.blayout}"

    @property
    def source(self) -> "Entry":
        """The FP16 order the mover converts from."""
        return Entry(self.groups, self.blocks)

    #: The framework's name for it: an order the card makes from another.
    derived_from = source

    def padded(self, shape: tuple) -> tuple:
        return Entry(self.groups, self.blocks).padded(shape)

    def nbytes(self, shape: tuple) -> int:
        rows, k = self.padded(shape)
        return rows * k * self.entry_bytes // (LANES * KBLOCK)

    def pack(self, array) -> bytes:
        arr = np.asarray(array, np.float16)
        if arr.ndim != 2:
            raise LayoutError(f"an operand is 2-d; got {arr.shape}")
        rows, k = self.padded(arr.shape)
        out = np.zeros((rows, k), np.float16)
        out[: arr.shape[0], : arr.shape[1]] = arr
        words = T.to_mxfp7_words_tiled(out, self.groups, self.blocks, self.blayout)
        return b"".join(w.to_bytes(WORD_BYTES, "little") for w in words)

    def unpack(self, raw: bytes, shape: tuple):
        rows, k = self.padded(shape)
        q, es, m8 = T.from_mxfp7_entries(raw[: self.nbytes(shape)], self.blayout)
        vals = q * (2.0 ** es[..., None]) * (m8[..., None] / 8.0)
        e = vals.reshape(
            rows // (self.groups * LANES),
            k // (self.blocks * KBLOCK),
            self.groups,
            self.blocks,
            LANES,
            KBLOCK,
        )
        e = e.transpose(0, 2, 4, 1, 3, 5).reshape(rows, k)
        return np.asarray(e[: shape[0], : shape[1]], np.float64)


def mx_inner(layout) -> MxEntry | None:
    """The :class:`MxEntry` `layout` holds -- itself, or a batched one's -- else None."""
    inner = getattr(layout, "inner", layout)
    return inner if isinstance(inner, MxEntry) else None


def mx_source(layout):
    """The FP16 order the mover converts an MXFP7 `layout` from, batched alike."""
    return layout.derived_from


def quantises(before, after) -> bool:
    """Whether `before -> after` is the mover's FP16 -> MXFP7 step itself."""
    return mx_inner(after) is not None and before.key == mx_source(after).key


def mx_runs(layout, shape: tuple) -> tuple:
    """``(count, stride, entries)``: the batch elements of an MXFP7 `layout`,
    their stride, and the FP16 entries the mover converts in each."""
    inner = mx_inner(layout)
    if inner is layout:
        return 1, 0, inner.source.nbytes(shape) // T.FP16_ENTRY_BYTES
    entries = inner.source.nbytes(layout.block) // T.FP16_ENTRY_BYTES
    return layout.count, layout.stride, entries


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
class PadHWC:
    """An ``[H][W][C]`` activation as a zero-padded ``[hp][wp][cp]`` FP16 image.

    The data sits at offset `pad` on both spatial axes; `cp` is C in whole 32s.
    """

    hp: int
    wp: int
    cp: int
    pad: int = 1

    @property
    def key(self) -> str:
        return f"hwc:{self.hp}x{self.wp}x{self.cp}:{self.pad}"

    def nbytes(self, shape: tuple) -> int:
        return self.hp * self.wp * self.cp * 2

    def pack(self, array) -> bytes:
        arr = np.asarray(array, np.float16)
        if arr.ndim != 3:
            raise LayoutError(f"an activation is [H][W][C]; got {arr.shape}")
        h, w, c = arr.shape
        p = self.pad
        if p + h > self.hp or p + w > self.wp or c > self.cp:
            raise LayoutError(f"{arr.shape} does not fit {self.key}")
        out = np.zeros((self.hp, self.wp, self.cp), np.float16)
        out[p : p + h, p : p + w, :c] = arr
        return out.tobytes()

    def unpack(self, raw: bytes, shape: tuple):
        h, w, c = shape
        p = self.pad
        img = np.frombuffer(raw, np.float16)[: self.hp * self.wp * self.cp]
        img = img.reshape(self.hp, self.wp, self.cp)
        return np.asarray(img[p : p + h, p : p + w, :c], np.float64)


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
