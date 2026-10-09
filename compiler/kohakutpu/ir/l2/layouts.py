"""KohakuTPU's buffer layouts (docs/projects/kohakutpu/ir/l2.md §1): each its
byte size, the byte offset of the pieces a work item reads or writes, and the
host's pack/unpack."""

from dataclasses import dataclass

import numpy as np
from kohakutpu.ir.l1.kernels import conv2d as CV
from kohakutpu.ir.l1.kernels import matmul as MM

ENTRY = MM.ENTRY
SUBTILE = MM.SUBTILE


@dataclass(frozen=True)
class MxA:
    """MXFP7 A operand `(rows, k)`, tile-major for `gm` x `nk`."""

    rows: int
    k: int
    gm: int
    nk: int

    @property
    def nbytes(self) -> int:
        return self.rows * self.k

    @property
    def chunks(self) -> int:
        return self.k // (32 * self.nk)

    def tile(self, i: int) -> tuple:
        """``(offset, nbytes)`` of row tile `i`, every K-chunk."""
        span = self.gm * self.nk * self.chunks * ENTRY
        return i * span, span

    def chunk(self, i: int, c: int) -> tuple:
        """``(offset, entries)`` of row tile `i`'s K-chunk `c`."""
        off, _ = self.tile(i)
        return off + c * self.gm * self.nk * ENTRY, self.gm * self.nk

    def pack(self, x) -> bytes:
        return MM.pack_a(x, self.gm, self.nk)


@dataclass(frozen=True)
class MxB(MxA):
    """MXFP7 B operand `(rows, k)`, tile-major for `gn` (stored as `gm`) x `nk`."""

    def pack(self, x) -> bytes:
        return MM.pack_b(x, self.gm, self.nk)


@dataclass(frozen=True)
class Tiles:
    """Drained sub-tiles of `(rows, cols)`, tile-major: tile (i, j) of
    ``gm x gn`` sub-tiles at ``(i*tn + j) * gm*gn*32``."""

    rows: int
    cols: int
    gm: int
    gn: int

    @property
    def tn(self) -> int:
        return self.cols // (4 * self.gn)

    @property
    def nbytes(self) -> int:
        return self.rows * self.cols * 2

    def tile(self, i: int, j: int) -> tuple:
        span = self.gm * self.gn * SUBTILE
        return (i * self.tn + j) * span, span

    def unpack(self, get, base: int) -> np.ndarray:
        tm = self.rows // (4 * self.gm)
        where = [
            (i * self.gm, self.gm, j, base + self.tile(i, j)[0])
            for i in range(tm)
            for j in range(self.tn)
        ]
        return MM.unpack(get, where, self.rows, self.cols, self.gm, self.gn)


@dataclass(frozen=True)
class Flat:
    """`n` fp16 elements in order."""

    n: int

    @property
    def nbytes(self) -> int:
        return self.n * 2

    def pack(self, x) -> bytes:
        return np.ascontiguousarray(x, np.float16).tobytes()

    def unpack(self, raw: bytes) -> np.ndarray:
        return np.frombuffer(raw, np.float16).astype(np.float64)


@dataclass(frozen=True)
class Rows(Flat):
    """fp16 `(n // cols, cols)` row-major."""

    cols: int = 16

    def unpack(self, raw: bytes) -> np.ndarray:
        return super().unpack(raw).reshape(-1, self.cols)


@dataclass(frozen=True)
class BandLane:
    """Conv input `(h, w, c)` as `conv2d.pack_input` lays it for `gm`, `cbc`."""

    h: int
    w: int
    c: int
    gm: int
    cbc: int

    @property
    def nbytes(self) -> int:
        return CV._rows(self.h, self.w, self.gm) * self.c * 4

    @property
    def chunk_bytes(self) -> int:
        return CV._rows(self.h, self.w, self.gm) * self.cbc * ENTRY

    def pack(self, x) -> bytes:
        return CV.pack_input(x, self.gm, self.cbc)


@dataclass(frozen=True)
class ConvB:
    """Conv weights `(cout, cin, 3, 3)` as `conv2d.pack_weights` lays them."""

    cout: int
    cin: int
    gn: int
    cbc: int

    @property
    def nbytes(self) -> int:
        return self.cout * self.cin * 9

    @property
    def tile_bytes(self) -> int:
        return self.gn * 9 * self.cin // 32 * ENTRY

    def pack(self, wt) -> bytes:
        return CV.pack_weights(wt, self.gn, self.cbc)
