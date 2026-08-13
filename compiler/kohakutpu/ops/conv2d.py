"""3x3 convolution, as a matmul whose K sweep walks the taps. NO bias, no act.

A tensor-level operator: one pass, one operation. Fused with a bias or an
activation it is a KERNEL and lives in `kernels/conv2d.py`.

`.plan/CONV2D.md` §4 branch C has the derivations. Three facts it does not:

* `nk` is FORCED to 1 -- two channel blocks of one pixel are `plane*64` bytes
  apart -- so a pass is `3*(9C/32) + 1` flits and cannot amortise;
* **the tile is the lever.** A layer is 22-26k flits near `gm*gn <= TILES` and
  **45-47 M** at `gm=2, gn=1`;
* a tapped fill straddles a 4 KB boundary on 6 of 9 taps, 1 burst in 16.
  UNTESTED ON SILICON: `mag_mem_port.v` has no split logic. Parked.
"""

from kohakuaccel.lang import ceildiv, dims, loop, units
from kohakutpu.lang import kernel

from kohakutpu import lang as L

H, W, C, N, K = dims("H, W, C, N, K")

#: The padded plane, rounded to whole entries. The result has a row per position
#: of it, of which `(y, x)` with `y < H` and `x < W` are the wanted outputs.
PLANE = ceildiv((H + 2) * (W + 2), L.LANES) * L.LANES


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


def positions(h: int, w: int):
    """Result rows carrying a real output, as ``(row, y, x)``.

    The rest of `c` is the padded raster the sweep had to run; see the header.
    """
    wp = w + 2
    return [(y * wp + x, y, x) for y in range(h) for x in range(w)]
