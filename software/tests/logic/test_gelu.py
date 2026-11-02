"""Tanh-form GELU in lane arithmetic: the negative tail keeps its value."""

import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.native import linear
from kohakutpu.language.numerics import e8m15

TAIL = np.array([-4.25, -4.5, -5.0, -1.0, 0.5, 3.0])


def lanes(e, x):
    """`e` over `x` with every operation's result rounded to E8M15."""
    if isinstance(e, X.Load):
        return e8m15.value(x)
    if isinstance(e, X.Const):
        return e8m15.value(e.value)
    args = [lanes(a, x) for a in e.args]
    loads = tuple(X.Load(i) for i in range(len(args)))
    return e8m15.value(X.evaluate(X.Op(e.name, loads), args))


def tanh_gelu(x):
    return 0.5 * x * (1 + np.tanh(np.sqrt(2 / np.pi) * (x + 0.044715 * x**3)))


def test_the_negative_tail_is_not_cancelled_in_lane_arithmetic():
    got = lanes(X.gelu(X.Load(0)), TAIL)

    want = tanh_gelu(TAIL)
    np.testing.assert_allclose(got, want, rtol=2e-3)


def test_the_fused_linear_epilogue_is_that_gelu():
    value = X.op("add", X.Load(0), X.Load(1))
    assert linear.epilogue("gelu", False) == X.gelu(value)
