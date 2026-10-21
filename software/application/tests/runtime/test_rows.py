"""Rows computations wider than one column block, compiled and checked on
an offline device: a reduction that is not the one result is computed
first, as a statistic the rest reads."""

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import rows as RW


@pytest.fixture(scope="module")
def dev():
    return D.Device(kind="offline")


def test_two_reductions_across_blocks_compile(dev):
    R, W = 8, 4 * V.MAX_BLOCK
    x = X.Load(0)
    mean = X.op("mul", X.Reduce("sum", x), 1.0 / W)
    d = X.op("sub", x, mean)
    var = X.op("mul", X.Reduce("sum", X.op("mul", d, d)), 1.0 / W)
    e = X.op("mul", d, X.op("rsqrt", X.op("add", var, 1e-5)))
    src, out = dev.alloc(2 * R * W), dev.alloc(2 * R * W)
    runs = RW.run(
        dev,
        e,
        [Operand("full", pitch=W, rows=R)],
        {"in0": src},
        R,
        W,
        Operand("full", pitch=W),
        out,
    )
    # The mean, then the variance (each a statistic), then the rows.
    assert len(runs) >= 3


def _addr(o: Operand, at: int, r: int, c: int) -> int:
    """The byte the kernel reads for row r, column c of operand `o` bound at
    `at` (`kernels.vector`: full / col by element, row / scalar by 32-wide
    statistic rows)."""
    item = 4 if o.dtype == "f32" else 2
    if o.kind in ("row", "scalar"):
        return at + item * (32 * (o.offset + r) + c)
    if o.kind == "col":
        return at + item * (o.offset + r)
    return at + item * (o.offset + r * o.pitch + c)


@pytest.mark.parametrize(
    "o",
    [
        Operand("full", pitch=96, offset=3 * 96 + 32, rows=10),
        Operand("full", pitch=48, offset=7, rows=4),
        Operand("full", "f32", pitch=64, offset=8, rows=4),
        Operand("col", offset=48),
        Operand("row", offset=5, rows=9),
        Operand("scalar", offset=2),
    ],
)
def test_an_offset_moves_into_the_address_reading_the_same_bytes(o):
    """`_based`: a whole-word offset into the bound address, every element
    read at the byte it was, the rows held past the new start kept; one off
    a word left as it was."""
    (got,), binds = RW._based([o], {"in0": 4096}, "in")
    for r, c in ((0, 0), (1, 5), (2, 31)):
        assert _addr(got, binds["in0"], r, c) == _addr(o, 4096, r, c)
    item = 4 if o.dtype == "f32" else 2
    whole = (o.offset * (32 if o.kind in ("row", "scalar") else 1) * item) % 32 == 0
    assert (got.offset == 0) == whole
    if whole and o.kind in ("full", "row"):
        start = o.offset // o.pitch if o.kind == "full" else o.offset
        assert got.rows == o.rows - start
