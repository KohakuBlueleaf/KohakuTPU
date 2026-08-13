"""3x3 convolution with its bias folded in.

A bias ALWAYS follows a conv, so this is structural rather than a guessed
fusion. The plain conv is an op -- `kohakutpu.ops.conv2d`. No activation here:
SDXL's ResNet block is `silu -> norm -> conv`, so a conv-then-activation kernel
would be fitting the wrong shape.

The bias rides a temp because THE EMITTER has no fill for a second epilogue
operand -- `ResidentEpilogueKernel` takes the resident tile and constants only.
That is a compiler gap, not a hardware one: the core uses 3 of 8 descriptors
here and `vec_agu`'s 4 dimensions are exactly what a per-channel read needs.
The shape this should have is at the bottom of the file.
"""

from kohakuaccel.lang import dims, loop, units
from kohakutpu.lang import kernel
from kohakutpu.ops.conv2d import PLANE

from kohakutpu import lang as L

H, W, C, N, K = dims("H, W, C, N, K")


@kernel
def conv2d_bias(
    a=L.In(..., H, W, C),
    b=L.In(N, K),
    bias=L.In(..., PLANE, N),
    c=L.Out(..., PLANE, N),
    *,
    gm=16,
    gn=32,
    part=8192,
):
    """``conv2d(a, b) + bias``. `bias` arrives at FULL shape.

    Per-channel would need an address-dependent read; an elementwise pass takes
    operands of one length, so the caller broadcasts it once on the host.
    """
    h = L.temp(PLANE, N)
    x = L.plane(a, gm)
    with units(x.tiles(gm), b.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, 1)
        for s in loop(b.chunks32(1)):
            acc += x[i, 0, x.tap(s)] @ b[j, s]
        h[i, j] <<= acc

    with units(h.parts(part)) as e:
        c[e] <<= h[e] + bias[e]


"""
An expected shape of the kernel, our compiler or hardware should support this

@kernel
def conv2d_bias(
    a=L.In(..., H, W, C),
    b=L.In(N, K),
    bias=L.In(N),
    c=L.Out(..., PLANE, N),
    *,
    gm=16,
    gn=32,
    part=8192,
):
    h = L.temp(PLANE, N)
    x = L.plane(a, gm)
    with units(x.tiles(gm), b.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, 1)
        for s in loop(b.chunks32(1)):
            acc += x[i, 0, x.tap(s)] @ b[j, s]
        h[i, j] <<= acc + bias[j]
"""
