"""3x3 convolution, as a matmul whose K sweep walks the taps. NO bias, no act.

A tensor-level operator: one pass, one operation. Fused with a bias or an
activation it is a KERNEL and lives in `kernels/conv2d.py`.

Branch C, whose derivations `compiler/tests/test_conv2d_branch_c.py` checks
against a hand-computed schedule. Three facts those do not carry:

* `nk` is FORCED to 1 -- two channel blocks of one pixel are `plane*64` bytes
  apart -- so a pass is `3*(9C/32) + 1` flits and cannot amortise;
* **the tile is the lever.** A layer is 22-26k flits near `gm*gn <= TILES` and
  **45-47 M** at `gm=2, gn=1`;
* a tapped fill straddles a 4 KB boundary on 6 of 9 taps, 1 burst in 16.
  UNTESTED ON SILICON: `mag_mem_port.v` has no split logic. Parked.
"""

from kohakuaccel.lang import ceildiv, dims, loop, units
from kohakutpu.hw import tensor as T
from kohakutpu.lang import kernel

from kohakutpu import lang as L
from kohakutpu import layout as LO

H, W, C, N, K = dims("H, W, C, N, K")

#: The padded plane, rounded to whole entries. The result has a row per position
#: of it, of which `(y, x)` with `y < H` and `x < W` are the wanted outputs.
PLANE = ceildiv((H + 2) * (W + 2), L.LANES) * L.LANES

#: The same for stride 2: the OUTPUT raster is one residue sub-plane's.
PLANE2 = ceildiv((ceildiv(H, 2) + 2) * (ceildiv(W, 2) + 2), L.LANES) * L.LANES


@kernel
def conv2d(a=L.In(..., H, W, C), b=L.In(N, K), c=L.Out(..., PLANE, N), *, gm=16, gn=32):
    """3x3 conv, pad 1. `a` is `[H][W][C]`, `b` is `[N][9C]`, `c` is `[plane][N]`.

    `a`'s leading axes are a batch the compiler makes a grid axis; `b` has no
    `...`, so ONE set of weights is shared by every element.

    `b`'s K axis runs (tap, channel block, channel) -- a host-side permute of
    torch's `(out, in, kh, kw)`, which :func:`weights_for_k` does.

    The sweep runs the PADDED raster, because four adjacent outputs are four
    adjacent inputs only WITHIN a row. :func:`positions` says which rows of `c`
    carry an output; the other **3.1% (128x128) to 12.9% (32x32)** are computed
    and discarded, which is also what makes zero padding free.

    `gm*gn` is 512, the conservative `TILES`; a URAM-tiled top allows 4096 and
    wants roughly `gm=51, gn=80` at 128x128x320 (`isa/cluster.md` §4.6).
    """
    x = L.plane(a, gm)
    with units(x.tiles(gm), b.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, 1)
        for s in loop(b.chunks32(1)):
            acc += x[i, 0, x.tap(s)] @ b[j, s]
        c[i, j] <<= acc


class _StrideTap:
    """Sweep step `s`'s tap offset when the convolution strides.

    The stride-1 :class:`kohakutpu.lang.Tap` with :func:`hw.tensor.stride_tap`
    for the offset, and identical to it at `stride == 1`. Rebinds itself, the
    hook `record._value` provides, because `//` is not index arithmetic.
    """

    def __init__(self, step, blocks, plane, wp, stride) -> None:
        self.step, self.blocks, self.plane, self.wp = step, blocks, plane, wp
        self.stride = stride

    def rebind(self, value):
        step, blocks = int(value(self.step)), int(value(self.blocks))
        plane, wp = int(value(self.plane)), int(value(self.wp))
        tap, block = divmod(step, blocks)
        dy, dx = divmod(tap, 3)
        sub = plane // (self.stride * self.stride)
        return block * plane + T.stride_tap(dy, dx, wp, sub, self.stride)


class StridePlane(L.Plane):
    """A `[H][W][C]` activation read as `stride*stride` residue sub-planes.

    Everything a :class:`kohakutpu.lang.Plane` is, over a plane the packer split
    by residue, so the sweep walks the OUTPUT raster and a tap is a constant
    into whichever sub-plane it lands in.
    """

    def __init__(self, buffer, gm: int, stride: int) -> None:
        self.buffer, self.stride = buffer, stride
        self.order = LO.ConvEntry(self.PAD, gm, stride)

    @property
    def rows(self):
        """Output rows: the sub-plane's, which is what the sweep walks."""
        return ceildiv(self.buffer.trailing[0], self.stride)

    @property
    def cols(self):
        return ceildiv(self.buffer.trailing[1], self.stride)

    @property
    def wp(self):
        return self.cols + 2 * self.PAD

    @property
    def plane(self):
        """Positions ONE channel block occupies: every sub-plane of it."""
        sub = ceildiv((self.rows + 2 * self.PAD) * self.wp, L.LANES) * L.LANES
        return sub * self.stride * self.stride

    @property
    def groups(self):
        """Lane groups worth sweeping, in the OUTPUT raster."""
        return ceildiv((self.rows - 1) * self.wp + self.cols, L.LANES)

    def tap(self, step) -> _StrideTap:
        return _StrideTap(step, self.blocks, self.plane, self.wp, self.stride)


def plane_strided(buffer, gm: int, stride: int) -> StridePlane:
    """`buffer` read as a strided convolution's residue-split activation."""
    return StridePlane(buffer, gm, stride)


@kernel
def conv2d_stride2(
    a=L.In(..., H, W, C), b=L.In(N, K), c=L.Out(..., PLANE2, N), *, gm=16, gn=32
):
    """3x3 conv, STRIDE 2, pad 1. The same sweep; the plane is residue-split.

    `a` is `[H][W][C]`, `b` is `[N][9C]` as :func:`weights_for_k` orders it, and
    `c` is `[sub-plane][N]` -- one row per output position, which
    :func:`positions` reads with `stride=2`.

    Identical to :func:`conv2d` but for the layout the activation is packed in,
    which is the whole content of `conv2d.md` 4.1: the alternative is computing
    every output and discarding three in four, at 4.0x the MAC.
    """
    x = plane_strided(a, gm, 2)
    with units(x.tiles(gm), b.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, 1)
        for s in loop(b.chunks32(1)):
            acc += x[i, 0, x.tap(s)] @ b[j, s]
        c[i, j] <<= acc


class _UpTap:
    """Tap offset for one residue class of a nearest-2x upsample.

    Four taps rather than nine, at ordinary stride-1 offsets shifted by the
    output's residue class -- see :func:`conv2d_upsample2`. Rebinds itself
    because `//` is not index arithmetic.
    """

    def __init__(self, step, blocks, plane, wp, iy, ix) -> None:
        self.step, self.blocks, self.plane, self.wp = step, blocks, plane, wp
        self.iy, self.ix = iy, ix

    def rebind(self, value):
        step, blocks = int(value(self.step)), int(value(self.blocks))
        tap, block = divmod(step, blocks)
        ty, tx = divmod(tap, 2)
        return (
            block * int(value(self.plane))
            + (self.iy + ty) * int(value(self.wp))
            + (self.ix + tx)
        )


@kernel
def conv2d_upsample2(
    a=L.In(..., H, W, C),
    b=L.In(N, K),
    c=L.Out(..., PLANE, N),
    *,
    iy=0,
    ix=0,
    gm=16,
    gn=32,
):
    """ONE residue class of `Conv3x3(nearest2x(a))`, without the 2x activation.

    Output `(2y+iy, 2x+ix)` reads `a[y + (iy+dy-1)//2][x + (ix+dx-1)//2]`, and
    over `dy` that takes TWO values -- so each output residue class is a 2x2
    convolution on the ORIGINAL plane at stride-1 tap offsets shifted by
    `(iy, ix)`, with weights :func:`weights_for_upsample2` folds at load.

    Four calls of four taps: 16 MAC per input pixel against 36 for materialising
    the 2x activation, which is never written. The four results ARE the residue
    split of the `[2H][2W][N]` output, in `ConvEntry(step=2)` order.
    """
    x = L.plane(a, gm)
    with units(x.tiles(gm), b.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, 1)
        for s in loop(b.chunks32(1)):
            acc += x[i, 0, _UpTap(s, x.blocks, x.plane, x.wp, iy, ix)] @ b[j, s]
        c[i, j] <<= acc


def weights_for_upsample2(kernel_hw, cin: int):
    """`(out, in, 3, 3)` as the four `[N][4C]` operands the four classes want.

    Returns `[4][N][4C]`, class `iy*2 + ix`. A tap of the upsampled convolution
    that lands on the same input pixel as another is ADDED to it, which is the
    whole fold and is exact: `iy=0` pairs `dy` 1 and 2, `iy=1` pairs 0 and 1.

    The tap index is `(iy+dy-1)//2 + 1 - iy`, which is the input row offset put
    back on 0..1 -- the `- iy` is what the kernel adds again as the class shift.
    """
    import numpy as np

    k = np.asarray(kernel_hw)
    out = np.zeros((4, k.shape[0], 4 * cin), k.dtype)
    for iy in range(2):
        for ix in range(2):
            for dy in range(3):
                for dx in range(3):
                    ty = (dy - 1 + iy) // 2 + 1 - iy
                    tx = (dx - 1 + ix) // 2 + 1 - ix
                    at = (ty * 2 + tx) * cin
                    out[iy * 2 + ix, :, at : at + cin] += k[:, :, dy, dx]
    return out


def weights_for_k(kernel_hw, cin: int):
    """`(out, in, kh, kw)` as the `[N][9C]` operand this sweep's K order wants.

    Step `s` is tap `s // (C/32)` at channel block `s % (C/32)`, so the taps are
    the outer axis of K. Done once on the host; it moves no more bytes than the
    upload does anyway.
    """
    import numpy as np

    k = np.asarray(kernel_hw)
    out = np.zeros((k.shape[0], 9 * cin), k.dtype)
    for t in range(9):
        dy, dx = divmod(t, 3)
        out[:, t * cin : (t + 1) * cin] = k[:, :, dy, dx]
    return out


def positions(h: int, w: int, stride: int = 1):
    """Result rows carrying a real output, as ``(row, y, x)``.

    The rest of `c` is the padded raster the sweep had to run; see the header.
    At `stride > 1` the raster is one residue SUB-PLANE's, so the rows are the
    same expression over `ceil(h/stride)` by `ceil(w/stride)` outputs.
    """
    hs, ws = -(-h // stride), -(-w // stride)
    wp = ws + 2
    return [(y * wp + x, y, x) for y in range(hs) for x in range(ws)]
