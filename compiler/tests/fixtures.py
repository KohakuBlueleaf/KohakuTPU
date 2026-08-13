"""Kernels the COMPILER TESTS own, so no compiler test imports a shipped one.

Rule 2b. A shipped kernel drags its own tiling choices into a compiler test, and
then a library change breaks that test and says nothing about the compiler. The
corpus here is chosen for what it makes the compiler DO, not for what it
computes -- between them these cover no temps, several with disjoint lifetimes,
a fused epilogue, a row reduction naming no coordinate, and a table.

Nothing here is meant to be fast, and nothing here ships.
"""

import numpy as np
from kohakuaccel.lang import dims, loop, units
from kohakutpu.lang import kernel

from kohakutpu import lang as L

M, K, N = dims("M, K, N")
LOG2E = 1.4426950408889634


@kernel
def mm(
    a=L.In(..., M, K), b=L.In(N, K), y=L.Out(..., M, N), *, gm=8, gn=8, nk=2, part=8192
):
    """Clusters only, no temp, no vector stage. The kernel a bare mesh can run."""
    with units(a.tiles(gm), b.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(a.chunks32(nk)):
            acc += a[i, k] @ b[j, k]
        y[i, j] <<= acc


@kernel
def mm_silu(
    a=L.In(..., M, K), b=L.In(N, K), y=L.Out(..., M, N), *, gm=8, gn=8, nk=2, part=8192
):
    """An epilogue on the accumulator, so the drain NAMES a vector core."""
    with units(a.tiles(gm), b.tiles(gn)) as (i, j):
        acc = L.tile(gm, gn, nk)
        for k in loop(a.chunks32(nk)):
            acc += a[i, k] @ b[j, k]
        y[i, j] <<= acc * L.recip(L.exp2(acc * -LOG2E) + 1.0)


@kernel
def rownorm(x=L.In(..., M, N), y=L.Out(..., M, N), *, eps=1e-5):
    """A row reduction, which names NO coordinate and so used to compile
    anywhere and die at dispatch.

    Three temps, each live from the stage that writes it to the stage that
    reads it.
    """
    sq, total, inv = L.temp(M, N), L.temp(M, N), L.temp(M, N)
    sq <<= (x / N) * x
    total <<= L.row_sum(sq)
    inv <<= L.rsqrt(total + eps)
    y <<= x * inv


@kernel
def chained(x=L.In(..., M, N), y=L.Out(..., M, N), *, eps=1e-5):
    """Passes whose first and third temps never coexist.

    What reuse has to find: `mu` is dead before `var` is born, so an allocator
    packing by lifetime puts them on the same bytes and one that does not
    cannot tell. The reductions are what hold the passes apart -- an
    elementwise chain fuses into one stage and then every temp is whole-run.
    """
    a, b, c, d = (L.temp(M, N) for _ in range(4))
    a <<= L.row_sum(x)
    b <<= x * L.rsqrt(a + eps)
    c <<= L.row_sum(b)
    d <<= b * L.rsqrt(c + eps)
    y <<= d + x


@kernel
def masked(x=L.In(..., M, N), y=L.Out(..., M, N), *, block=64):
    """A table: ONE copy every batch element reads, so it is not batched.

    Sizing it by the full shape over-reports it by the batch factor, which is
    the whole reason this fixture is here.
    """
    bias = np.triu(np.full((block, block), -1e4, np.float16), 1)
    y <<= x + L.table(bias)


@kernel
def staged_norm(
    x=L.In(..., M, N),
    w=L.In(..., M, N),
    b=L.In(..., M, N),
    y=L.Out(..., M, N),
    *,
    eps=1e-5,
):
    """Seven temps over eight passes, so some are provably never live together.

    A pass at a time on purpose: the fused form has no temp at all and is the
    wrong vehicle for a test about sharing them.
    """
    total, mu, dev = L.temp(M, N), L.temp(M, N), L.temp(M, N)
    sq, var, inv, scaled = (L.temp(M, N) for _ in range(4))
    total <<= L.row_sum(x)
    mu <<= total / N
    dev <<= x - mu
    sq <<= (dev / N) * dev
    var <<= L.row_sum(sq)
    inv <<= L.rsqrt(var + eps)
    scaled <<= dev * inv
    y <<= scaled * w + b


@kernel
def scale(x=L.In(...), y=L.Out(...)):
    """One pass at any rank, so a shape can be small enough to test alignment."""
    y <<= x * 2.0


@kernel
def residual(a=L.In(...), b=L.In(...), y=L.Out(...)):
    """Two ports, so one tensor can be passed as both and prove that is legal."""
    y <<= a + b


#: Every fixture, with operand shapes and knobs that compile.
CORPUS = [
    ("mm", mm, [(64, 128), (64, 128)], {}),
    ("mm_silu", mm_silu, [(64, 128), (64, 128)], {}),
    ("rownorm", rownorm, [(64, 64)], {}),
    ("chained", chained, [(64, 64)], {}),
    ("masked", masked, [(2, 64, 64)], {"block": 64}),
    ("scale", scale, [(64, 64)], {}),
]


def operands(dev, shapes, seed=11):
    """Deterministic fp16 tensors on `dev`, one per shape."""
    rng = np.random.default_rng(seed)
    return [dev.tensor(np.asarray(rng.normal(0, 1, s), np.float16)) for s in shapes]


class Shaped:
    """What `iface.solve` needs of an argument, without a device to put it on."""

    def __init__(self, shape) -> None:
        self.shape = shape
