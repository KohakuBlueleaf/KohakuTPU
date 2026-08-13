"""The contraction, plain: no epilogue, no bias, no activation.

Fused with any of those it is a KERNEL and lives in `kernels/`. This is the one
a tinygrad backend must have.
"""

from kohakuaccel.lang import dims, loop, units
from kohakutpu.lang import kernel

from kohakutpu import lang as L

M, K, N = dims("M, K, N")


@kernel
def matmul(a=L.In(..., M, K), b=L.In(N, K), c=L.Out(..., M, N), *, gm=8, gn=8, nk=2):
    """``C = a @ b.T``. `b` is stored ``[N][K]``, as a torch Linear keeps it.

    `a`'s leading axes are a batch the compiler makes a grid axis; `b` has no
    `...`, so one weight is shared by every element. `gm`/`gn` size the
    accumulator in sub-tiles and `nk` the K-blocks per sweep; intensity is
    `2*gm*gn/(gm+gn)` and MXFP7 quantises per 32-element K-block, so a wider
    tile costs no accuracy -- measured identical at 2x1, 4x4 and 8x8.
    """
    with units(a.tiles(gm), b.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(a.chunks32(nk)):
            acc += a[i, k] @ b[j, k]
        c[i, j] <<= acc
