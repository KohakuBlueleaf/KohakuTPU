"""`api` picks the kernel; the caller picks nothing. The "near no control" end.

`VRED` folds at most VLMAX lanes, so a row past it needs the hierarchical
kernel -- and a KERNEL CANNOT CHOOSE. While tracing, an extent is a `Name` and
`x.cols > 128` raises `TypeError`. Here `x.shape` is a concrete tuple, which is
the whole reason the dispatch lives at this level and not one down.

A DiT's model dim is 1024, so both sides of the branch are real shapes.
"""

import numpy as np
import pytest
from kohakutpu.kernels.groupnorm import VLMAX

from kohakutpu import api

NARROW, WIDE = 64, 1024


def arr(shape, seed=1, loc=0.0, scale=1.0):
    rng = np.random.default_rng(seed)
    return np.asarray(rng.normal(loc, scale, shape), np.float16)


def put(*arrays):
    d = api.script_device("sim")
    return [d.tensor(a) for a in arrays]


def f64(t):
    return np.float64(np.asarray(t.numpy()))


@pytest.mark.parametrize("cols", [NARROW, WIDE], ids=["narrow", "wide"])
def test_softmax_is_one_call_at_either_width(cols):
    x = arr((4, cols))
    got = f64(api.softmax(*put(x)))
    ref = np.exp(np.float64(x) - np.float64(x).max(-1, keepdims=True))
    ref /= ref.sum(-1, keepdims=True)
    assert np.allclose(got.sum(-1), 1.0, atol=2e-3)
    assert np.abs(got - ref).max() / ref.max() < 5e-3


@pytest.mark.parametrize("cols", [NARROW, WIDE], ids=["narrow", "wide"])
def test_rmsnorm_is_one_call_at_either_width(cols):
    x, w = arr((4, cols)), arr((4, cols), seed=2, loc=1.0, scale=0.1)
    got = f64(api.rmsnorm(*put(x, w)))
    xf = np.float64(x)
    ref = xf / np.sqrt((xf**2).mean(-1, keepdims=True) + 1e-5) * np.float64(w)
    assert np.abs(got - ref).max() / np.abs(ref).max() < 5e-3


@pytest.mark.parametrize("cols", [NARROW, WIDE], ids=["narrow", "wide"])
def test_layernorm_is_one_call_at_either_width(cols):
    x = arr((4, cols))
    w = arr((4, cols), seed=2, loc=1.0, scale=0.1)
    b = arr((4, cols), seed=3, scale=0.1)
    got = f64(api.layernorm(*put(x, w, b)))
    xf = np.float64(x)
    mu = xf.mean(-1, keepdims=True)
    var = ((xf - mu) ** 2).mean(-1, keepdims=True)
    ref = (xf - mu) / np.sqrt(var + 1e-5) * np.float64(w) + np.float64(b)
    assert np.abs(got - ref).max() / np.abs(ref).max() < 5e-3


def test_the_branch_is_the_reduction_width_and_nothing_else():
    """Exactly at VLMAX is still ONE pass; one element past it is not."""
    (fits,) = put(arr((2, VLMAX)))
    (over,) = put(arr((2, VLMAX * 2)))
    assert api._fold(fits) is None
    assert api._fold(over) == 2


def test_a_row_that_does_not_split_is_REFUSED_not_silently_padded():
    """`split` raises rather than fold a partial pass, and api does not catch it."""
    (odd,) = put(arr((2, VLMAX + 8)))
    with pytest.raises(ValueError, match="does not split"):
        api.softmax(odd)


def test_the_method_and_the_function_are_one_path():
    """Two entry points that could disagree is how a caller gets the slow one."""
    x = arr((4, WIDE))
    (t,) = put(x)
    assert np.array_equal(f64(api.softmax(t)), f64(api.Array.softmax(t)))
