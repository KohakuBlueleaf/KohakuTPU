"""A statistic against a row broadcast on the model, through the Python DSL
(the route of a fused group's kernel and of a prepared one): each row reads
its own value, against CPU f32 arithmetic."""

import numpy as np
import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms as FM

ROWS = Operand("row", rows=32)
COL = Operand("col")
L0, L1, L2 = X.Load(0), X.Load(1), X.Load(2)


def statistic():
    return np.arange(1, 33, dtype=np.float16)


def column(W, step=100):
    return np.arange(W, dtype=np.float16) * step


def launch(dev, e, operands, inputs, R, W):
    """`e` over R rows of W: the bytes it writes."""
    out = Operand("full", pitch=W, rows=R)
    k, given, _ = V.rows_kernel(e, operands, R, W, out, dev.machine.count("VC"))
    addresses = [dev.alloc(x.nbytes) for x in inputs]
    o = dev.alloc(given["out"].nbytes())
    for at, x in zip(addresses, inputs):
        dev.write(at, x.tobytes())
    binds = {f"in{i}": at for i, at in enumerate(addresses)}
    try:
        FM.launch(dev, k, given, binds | {"out": o})
        dev.drain()
        return dev.read(o, R * W * 2)
    finally:
        for at in (*addresses, o):
            dev.free(at)


def f16(want):
    return want.astype(np.float16).tobytes()


GRID = [(R, W) for R in (4, 5, 16, 20, 32) for W in (32, 64)]


@pytest.mark.parametrize("R,W", GRID)
def test_each_row_reads_its_statistic(dev, R, W):
    y, c = statistic(), column(W)
    got = launch(dev, X.op("add", L0, L1), (ROWS, COL), (y, c), R, W)
    assert got == f16(y[:R, None].astype(np.float32) + c[None].astype(np.float32))


@pytest.mark.parametrize("R,W", [(16, 32), (20, 64)])
def test_a_second_band_does_not_cut_the_rows(dev, R, W):
    """`y[r] * a[j] + b[j]`, two bands to one statistic."""
    y, a, b = statistic(), column(W, 2), column(W, 3)
    got = launch(
        dev,
        X.op("add", X.op("mul", L0, L1), L2),
        (ROWS, COL, COL),
        (y, a, b),
        R,
        W,
    )
    want = y[:R, None].astype(np.float32) * a[None] + b[None]
    assert got == f16(want)


@pytest.mark.parametrize("R,W", [(16, 32), (20, 64)])
def test_a_band_product_keeps_its_rows_below_data(dev, R, W):
    """`m[r, j] + (a[j] * y[r])^2`: the band's product is typed apart from the
    data rows it is added to, and each row holds its own statistic."""
    m = np.arange(R * W, dtype=np.float16).reshape(R, W) % 64
    y, a = statistic(), (np.arange(W, dtype=np.float16) % 4)
    product = X.op("mul", L1, L2)
    got = launch(
        dev,
        X.op("add", L0, X.op("mul", product, product)),
        (Operand("full", pitch=W), COL, ROWS),
        (m, a, y),
        R,
        W,
    )
    band = a[None].astype(np.float32) * y[:R, None]
    assert got == f16(m.astype(np.float32) + band * band)


@pytest.mark.parametrize("R,W", [(5, 32), (16, 64)])
def test_a_lane_stays_one_value(dev, R, W):
    """A `lane=` scalar against a broadcast: every row is the band's row, the
    scalar lends none."""
    word = np.arange(16, dtype=np.float16) + 3
    c = column(W)
    scalar = Operand("scalar", offset=5)
    got = launch(dev, X.op("add", L0, L1), (scalar, COL), (word, c), R, W)
    assert got == f16(word[5].astype(np.float32) + np.tile(c, (R, 1)))
