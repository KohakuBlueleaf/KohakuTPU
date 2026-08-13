"""A matmul fused with the epilogue that follows it.

The `linear_*` family is one contraction and one epilogue. An epilogue reading
only the accumulator runs on a vector core over the NoC and the tile never goes
back through MAG. `fuse=False` stages it through a temp instead, and
`Kernel.relax` turns that knob itself when the grid outgrows the vector cores,
so the author writes the fused form once and never picks.

`b` is a RESIDUAL here -- full shape, because a residual is. A per-channel bias
is :func:`linear_bias`, which rides the tile and allocates nothing.

The staged family's second stage is written as a bare `y <<= ...` on purpose:
it is one pass over the WHOLE result, and a grid the compiler sizes is what
`Runtime.regrid` widens from 2 instances to 24 at the adaLN shape.
"""

from kohakuaccel.lang import dims, loop, units
from kohakutpu.lang import kernel

from kohakutpu import lang as L

M, K, N = dims("M, K, N")

#: `exp2` is the transcendental this core has. Fold this into a scale where there
#: is one: a separate multiply is a whole pass over the operand.
LOG2E = 1.4426950408889634

#: The tanh GELU's constants: `2 sqrt(2/pi)`, already doubled by the tanh
#: identity, and the cubic's weight. :func:`gelu` is the cheaper sigmoid one.
GELU_IN = 2 * 0.7978845608028654
GELU_CUBE = 0.044715


def sigmoid(v):
    """``1 / (1 + 2**(-log2e * v))`` -- in this core's opcodes."""
    return L.recip(L.exp2(v * -LOG2E) + 1.0)


def gelu_tanh(v):
    """``0.5 v (1 + tanh(sqrt(2/pi) (v + 0.044715 v^3)))``, as one chain.

    `tanh(z)` is `2 sigmoid(2z) - 1`, so the whole thing collapses to
    `v * sigmoid(2 sqrt(2/pi) (v + 0.044715 v^3))` -- the same number in five
    fewer ops, and one running result instead of a tree.
    """
    # Every constant is the SECOND operand and the two scales fold into one: an
    # S operand in the `va` slot has never been demonstrated.
    inner = v * v * v * GELU_CUBE + v
    return v * L.recip(L.exp2(inner * (-GELU_IN * LOG2E)) + 1.0)


@kernel
def linear_silu(
    x=L.In(..., M, K), w=L.In(N, K), y=L.Out(..., M, N), *, gm=8, gn=8, nk=2, fuse=True
):
    """``silu(x @ w.T)`` over any leading rank, with no intermediate behind MAG.

    `gm`/`gn` size the accumulator in sub-tiles, `nk` the K-blocks per sweep.
    With `fuse` the epilogue rides the drained tile; the grid must then be no
    wider than the vector cores and `2*gm*gn` L1 words must clear `require_l1`.
    """
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= acc * sigmoid(acc)


@kernel
def linear_relu(
    x=L.In(..., M, K), w=L.In(N, K), y=L.Out(..., M, N), *, gm=8, gn=8, nk=2, fuse=True
):
    """``relu(x @ w.T)`` over any leading rank, with no intermediate behind MAG."""
    # `(acc + |acc|) * 0.5`, not MAX: its operand slot has never been
    # demonstrated by a kernel that ran.
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= (acc + L.absolute(acc)) * 0.5


@kernel
def linear_gelu(
    x=L.In(..., M, K), w=L.In(N, K), y=L.Out(..., M, N), *, gm=8, gn=8, nk=2, fuse=True
):
    """``gelu(x @ w.T)`` over any leading rank, the TANH gelu a transformer means."""
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= gelu_tanh(acc)


@kernel
def linear_scale(
    x=L.In(..., M, K),
    w=L.In(N, K),
    y=L.Out(..., M, N),
    *,
    s=1.0,
    gm=8,
    gn=8,
    nk=2,
    fuse=True,
):
    """``(x @ w.T) * s``. `s` is attention's `1/sqrt(d)`, folded as a constant."""
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= acc * s


@kernel
def linear_bias(
    x=L.In(..., M, K),
    w=L.In(N, K),
    b=L.In(N),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
):
    """``x @ w.T + b`` with `b` PER-CHANNEL, riding the resident tile.

    `layout.ChannelBias` stores one word a column group and the epilogue reads
    it at stride 0, so this allocates no temp and costs no second pass. It
    CANNOT stage: a tile index does not survive the fallback, so a grid wider
    than the vector cores is refused rather than broadcast.
    """
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        y[i, j] <<= acc + b[j]


@kernel
def linear_add(
    x=L.In(..., M, K),
    w=L.In(N, K),
    b=L.In(..., M, N),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
):
    """``(x @ w.T) + b`` with `b` at FULL shape -- a residual, not a bias.

    Staged: the second operand is as long as the result, so it cannot ride the
    tile. For a per-channel bias use :func:`linear_bias`, which allocates
    nothing.
    """
    h = L.temp(M, N)
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        h[i, j] <<= acc

    y <<= h + b


@kernel
def linear_add_relu(
    x=L.In(..., M, K),
    w=L.In(N, K),
    b=L.In(..., M, N),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
):
    """``relu((x @ w.T) + b)`` with `b` at full shape."""
    # The biased tile is read TWICE, so `(t + |t|) * 0.5` is a tree: it is
    # lifted once into a register and the epilogue stays ONE pass.
    h = L.temp(M, N)
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        h[i, j] <<= acc

    t = h + b
    y <<= (t + L.absolute(t)) * 0.5


@kernel
def linear_add_silu(
    x=L.In(..., M, K),
    w=L.In(N, K),
    b=L.In(..., M, N),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
):
    """``silu((x @ w.T) + b)`` with `b` at full shape."""
    h = L.temp(M, N)
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        h[i, j] <<= acc

    t = h + b
    y <<= t * sigmoid(t)


@kernel
def linear_add_gelu(
    x=L.In(..., M, K),
    w=L.In(N, K),
    b=L.In(..., M, N),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
):
    """``gelu((x @ w.T) + b)`` with `b` at full shape, the TANH gelu."""
    # Five constants, so the band seeds them into registers rather than filling
    # five arrays; the biased tile is the one lifted chain.
    h = L.temp(M, N)
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        h[i, j] <<= acc

    y <<= gelu_tanh(h + b)


@kernel
def linear_gate_add(
    x=L.In(..., M, K),
    w=L.In(N, K),
    g=L.In(..., M, N),
    r=L.In(..., M, N),
    y=L.Out(..., M, N),
    *,
    gm=8,
    gn=8,
    nk=2,
):
    """``r + g * (x @ w.T)`` -- an adaLN-Zero block's gated residual.

    `g` scales the projection and `r` is what it rejoins. Both are full shape.
    """
    # Three operands is eight of eight descriptors, with the store shared: a
    # fourth would not fit and would have to be its own pass.
    h = L.temp(M, N)
    with units(x.tiles(gm), w.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(x.chunks32(nk)):
            acc += x[i, k] @ w[j, k]
        h[i, j] <<= acc

    y <<= h * g + r
