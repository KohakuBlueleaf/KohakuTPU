"""A statistic against a row broadcast on the model, through the Python DSL
(the route of a fused group's kernel): each row reads its own value."""

import numpy as np
import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms as FM


def computed(dev, R, W):
    """`y[r] + c[j]` over R rows of W, and what it must be."""
    e = X.op("add", X.Load(0), X.Load(1))
    operands = (Operand("row", rows=32), Operand("col"))
    out = Operand("full", pitch=W, rows=R)
    k, given, _ = V.rows_kernel(e, operands, R, W, out, dev.machine.count("VC"))
    y = np.arange(1, 33, dtype=np.float16)
    c = np.arange(W, dtype=np.float16) * 100
    a, b = dev.alloc(y.nbytes), dev.alloc(c.nbytes)
    o = dev.alloc(given["out"].nbytes())
    dev.write(a, y.tobytes())
    dev.write(b, c.tobytes())
    try:
        FM.launch(dev, k, given, {"in0": a, "in1": b, "out": o})
        dev.drain()
        got = dev.read(o, R * W * 2)
    finally:
        for at in (a, b, o):
            dev.free(at)
    want = y[:R, None].astype(np.float32) + c[None].astype(np.float32)
    return got, want.astype(np.float16).tobytes()


@pytest.mark.parametrize("R,W", [(32, 32), (16, 32), (16, 64), (20, 32)])
def test_each_row_reads_its_statistic(dev, R, W):
    got, want = computed(dev, R, W)
    assert got == want
