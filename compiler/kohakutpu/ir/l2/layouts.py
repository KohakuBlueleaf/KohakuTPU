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

    def pack(self, x) -> bytes:
        """`(rows, cols)` fp16 as the clusters drain it: tile (i, j), then
        sub-tile row and column, then a 4x4 sub-tile row-major in one word."""
        tm = self.rows // (4 * self.gm)
        a = np.asarray(x, np.float16).reshape(tm, self.gm, 4, self.tn, self.gn, 4)
        return np.ascontiguousarray(a.transpose(0, 3, 1, 4, 2, 5)).tobytes()

    def unpack(self, get, base: int) -> np.ndarray:
        tm = self.rows // (4 * self.gm)
        where = [
            (i * self.gm, self.gm, j, base + self.tile(i, j)[0])
            for i in range(tm)
            for j in range(self.tn)
        ]
        return MM.unpack(get, where, self.rows, self.cols, self.gm, self.gn)


@dataclass(frozen=True)
class OnesA:
    """The A side of the bias K-block (`matmul.ones_block`): `4*gm` rows of
    `matmul.BIAS_A`, one K-block, MXFP7."""

    gm: int

    @property
    def nbytes(self) -> int:
        return self.gm * ENTRY

    def pack(self, _=None) -> bytes:
        return MM.ones_block(self.gm)


@dataclass(frozen=True)
class BiasB:
    """The B side of the bias K-block (`matmul.bias_block`): `n` channels'
    bias as ``hi, lo`` against the quantised ones row, MXFP7, `gn` a tile."""

    n: int
    gn: int

    @property
    def nbytes(self) -> int:
        return self.n // 4 * ENTRY

    def pack(self, bias) -> bytes:
        return MM.bias_block(bias, self.gn)


@dataclass(frozen=True)
class TileCols:
    """`count` column vectors of `cols` fp16 as a drained tile's columns: for
    each column tile `j`, each vector's `gn` words, word `sc` holding columns
    ``4*(j*gn + sc) ..`` in every one of its four rows."""

    cols: int
    gn: int
    count: int

    @property
    def tn(self) -> int:
        return self.cols // (4 * self.gn)

    @property
    def nbytes(self) -> int:
        return self.tn * self.count * self.gn * SUBTILE

    def tile(self, j: int) -> tuple:
        span = self.count * self.gn * SUBTILE
        return j * span, span

    def pack(self, vectors) -> bytes:
        a = np.asarray(vectors, np.float16).reshape(self.count, self.tn, self.gn, 1, 4)
        a = np.broadcast_to(a, (self.count, self.tn, self.gn, 4, 4))
        return np.ascontiguousarray(a.transpose(1, 0, 2, 3, 4)).tobytes()


@dataclass(frozen=True)
class ConvTiles:
    """A 3x3 conv's `(h, w, cout)` result as `conv_tile` drains it: `Tiles` of
    the band-lane positions (``conv2d.geometry``), four bands a sub-tile row."""

    h: int
    w: int
    cout: int
    gm: int
    gn: int

    @property
    def tiles(self) -> Tiles:
        positions = CV.geometry(self.h, self.w, self.gm)[2]
        return Tiles(positions * CV.LANES, self.cout, self.gm, self.gn)

    @property
    def nbytes(self) -> int:
        return self.tiles.nbytes

    def tile(self, i: int, j: int) -> tuple:
        return self.tiles.tile(i, j)

    def pack(self, x) -> bytes:
        hs, wp, _ = CV.geometry(self.h, self.w, self.gm)
        t = self.tiles
        y = np.zeros((t.rows, self.cout), np.float16)
        xp = np.zeros((self.h, wp, self.cout), np.float16)
        xp[:, : self.w] = x
        for k in range(CV.LANES):
            y[k :: CV.LANES][: hs * wp] = xp[k * hs : (k + 1) * hs].reshape(
                -1, self.cout
            )
        return t.pack(y)

    def unpack(self, get, base: int) -> np.ndarray:
        t = self.tiles
        where = [
            (i * self.gm, self.gm, j, base + t.tile(i, j)[0])
            for i in range(t.rows // (4 * self.gm))
            for j in range(t.tn)
        ]
        return CV.unpack(get, where, self.h, self.w, self.cout, self.gm, self.gn)


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
