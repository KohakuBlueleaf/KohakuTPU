"""Convolution as an ordinary matmul over an im2col the mover builds on the card.

The result is ``[rows][C_out]``, row ``y * ow4 + x`` for output ``(y, x)``
(:func:`positions`). The walk, stride and upsampling are conv2d.md §2-4.
"""

from dataclasses import dataclass

import numpy as np
from kohakuaccel.package import mover as PM
from kohakutpu.ops.matmul import matmul

from kohakutpu import layout as LO

LANES = LO.LANES
KBLOCK = LO.KBLOCK
#: Walker levels: the source spends two on lane and word, so four for entries.
WALK_LEVELS = PM.NDIM - 2
MX_BYTES = 128
WORD = PM.WORD_BYTES


@dataclass(frozen=True)
class Geometry:
    """One convolution's shapes: input, taps, stride, shift, and what follows."""

    h: int
    w: int
    c: int
    kh: int = 3
    kw: int = 3
    stride: int = 1
    #: Origin shift of the window in the padded image (an upsample class's).
    sy: int = 0
    sx: int = 0
    pad: int = 1
    #: Output extent; None for the ordinary `(H + 2*pad - k) // stride + 1`.
    out: tuple | None = None

    @property
    def oh(self) -> int:
        return (
            self.out[0]
            if self.out
            else (self.h + 2 * self.pad - self.kh) // self.stride + 1
        )

    @property
    def ow(self) -> int:
        return (
            self.out[1]
            if self.out
            else (self.w + 2 * self.pad - self.kw) // self.stride + 1
        )

    @property
    def ow4(self) -> int:
        """Output width in whole lanes: a lane group never crosses a row."""
        return -(-self.ow // LANES) * LANES

    @property
    def cp(self) -> int:
        return -(-self.c // KBLOCK) * KBLOCK

    @property
    def k(self) -> int:
        return self.kh * self.kw * self.cp

    @property
    def rows(self) -> int:
        return self.oh * self.ow4

    def image(self) -> LO.PadHWC:
        """The padded image every tap of every lane reads inside."""
        s = self.stride
        hp = max((self.oh - 1) * s + self.kh + self.sy, self.h + self.pad)
        wp = max((self.ow4 - 1) * s + self.kw + self.sx, self.w + self.pad)
        return LO.PadHWC(hp, wp, self.cp, self.pad)

    def groups(self, gm: int) -> int:
        """Lane groups per tile: the largest divisor of a row's groups <= `gm`."""
        q = self.ow4 // LANES
        return max(d for d in range(1, min(gm, q) + 1) if q % d == 0)


def im2col_moves(g: Geometry, gt: int, src: int, dst: int) -> list:
    """The converting moves that write ``MxEntry(gt, 1, 0)`` of the im2col.

    Levels are ``(count, source stride, destination stride)`` (conv2d.md §3).
    """
    img = g.image()
    px = g.cp * 2  # one pixel's channels
    s = g.stride
    nb = g.cp // KBLOCK
    nch = g.kh * g.kw * nb
    tx = g.ow4 // (LANES * gt)
    levels = [
        (g.oh, s * img.wp * px, tx * nch * gt * MX_BYTES),
        (tx, LANES * gt * s * px, nch * gt * MX_BYTES),
        (g.kh, img.wp * px, g.kw * nb * gt * MX_BYTES),
        (g.kw, px, nb * gt * MX_BYTES),
        (nb, KBLOCK * 2, gt * MX_BYTES),
        (gt, LANES * s * px, MX_BYTES),
    ]
    levels = [lv for lv in levels if lv[0] > 1] or [(1, 0, 0)]
    hw = sorted(range(len(levels)), key=lambda i: -levels[i][0])[:WALK_LEVELS]
    hw = sorted(hw)
    sw = [i for i in range(len(levels)) if i not in hw]
    origin = src + (g.sy * img.wp + g.sx) * px
    inner = [(LANES, s * px), (KBLOCK * 2 // WORD, WORD)]
    out = []
    for idx in np.ndindex(*[levels[i][0] for i in sw]):
        so = sum(n * levels[i][1] for n, i in zip(idx, sw, strict=True))
        do = sum(n * levels[i][2] for n, i in zip(idx, sw, strict=True))
        sd = [(levels[i][0], levels[i][1]) for i in hw] + inner
        dd = [(levels[i][0], levels[i][2]) for i in hw]
        out.append(PM.convert_walk((origin + so, sd), (dst + do, dd)))
    return out


def lower(a, b, g: Geometry, gm: int, gn: int):
    """`a` ``[H][W][C]`` through the mover's im2col, then a matmul against `b`."""
    dev = a.dev
    gt = g.groups(gm)
    src = a.address(g.image())
    lay = LO.MxEntry(gt, 1, 0)
    nbytes = lay.nbytes((g.rows, g.k))
    col = dev.empty((g.rows, g.k), lay, tier=dev.mx_tier(nbytes, True))
    dst = col.buffers[lay.key].addr
    for writes in im2col_moves(g, gt, src, dst):
        dev.move(writes, "im2col")
    dev.counters["im2col"] = dev.counters.get("im2col", 0) + 1
    # No wider than the output channels: a later relayout has no walk for more.
    gn = max(1, min(gn, -(-b.shape[0] // LANES)))
    return matmul(col, b, gm=gt, gn=gn, nk=1)


def _check(a, b, g: Geometry) -> None:
    if len(a.shape) != 3:
        raise ValueError(f"a convolution's activation is [H][W][C]; got {a.shape}")
    if tuple(b.shape) != (b.shape[0], g.k):
        raise ValueError(
            f"weights are [C_out][{g.kh}*{g.kw}*{g.cp}] (weights_for_k pads C to "
            f"{g.cp}); got {tuple(b.shape)}"
        )


def conv2d(a, b, *, gm=16, gn=32):
    """3x3 convolution, stride 1, pad 1. `a` ``[H][W][C]``, `b` from :func:`weights_for_k`."""
    g = Geometry(*a.shape)
    _check(a, b, g)
    return lower(a, b, g, gm, gn)


def conv2d_stride2(a, b, *, gm=16, gn=32):
    """3x3 convolution, STRIDE 2, pad 1: the same walk at twice the pixel steps."""
    g = Geometry(*a.shape, stride=2)
    _check(a, b, g)
    return lower(a, b, g, gm, gn)


def conv2d_upsample2(a, b, *, iy=0, ix=0, gm=16, gn=32):
    """Residue class ``(iy, ix)`` of ``Conv3x3(nearest2x(a))`` (conv2d.md §4).

    `b` is that class's operand from :func:`weights_for_upsample2`.
    """
    h, w, c = a.shape
    g = Geometry(h, w, c, kh=2, kw=2, sy=iy, sx=ix, out=(h, w))
    _check(a, b, g)
    return lower(a, b, g, gm, gn)


def _padded_c(cin: int) -> int:
    return -(-cin // KBLOCK) * KBLOCK


def weights_for_k(kernel_hw, cin: int):
    """``(out, in, kh, kw)`` as ``[N][kh*kw*cp]``: tap-major, C padded to `cp`.

    The im2col's K order; the padded channels are zero on both sides.
    """
    k = np.asarray(kernel_hw)
    kh, kw = k.shape[2], k.shape[3]
    cp = _padded_c(cin)
    out = np.zeros((k.shape[0], kh * kw * cp), k.dtype)
    for t in range(kh * kw):
        dy, dx = divmod(t, kw)
        out[:, t * cp : t * cp + cin] = k[:, :, dy, dx]
    return out


def weights_for_upsample2(kernel_hw, cin: int):
    """``(out, in, 3, 3)`` as the four ``[N][4*cp]`` operands the four classes want.

    Returns ``[4][N][4*cp]``, class ``iy*2 + ix``; taps landing on one input
    pixel are added, at tap ``(iy+dy-1)//2 + 1 - iy``.
    """
    k = np.asarray(kernel_hw)
    cp = _padded_c(cin)
    out = np.zeros((4, k.shape[0], 4 * cp), k.dtype)
    for iy in range(2):
        for ix in range(2):
            for dy in range(3):
                for dx in range(3):
                    ty = (dy - 1 + iy) // 2 + 1 - iy
                    tx = (dx - 1 + ix) // 2 + 1 - ix
                    at = (ty * 2 + tx) * cp
                    out[iy * 2 + ix, :, at : at + cin] += k[:, :, dy, dx]
    return out


def positions(h: int, w: int, stride: int = 1, k: int = 3):
    """Result rows carrying a real output, as ``(row, y, x)``.

    Row ``y * ow4 + x``: the rest of each row is the lane padding the walk ran.
    """
    g = Geometry(h, w, 1, kh=k, kw=k, stride=stride)
    return [(y * g.ow4 + x, y, x) for y in range(g.oh) for x in range(g.ow)]
