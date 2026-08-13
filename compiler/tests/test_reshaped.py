"""`Buffer.as_rows`: the same bytes walked as rows of `width`.

Row-major `(M, N)` and `(M*N/W, W)` are one linear memory, so a reinterpretation
costs nothing and changes only what `VRED` folds over. `kernels/wide.py` makes
the CALLER do this by hand -- it declares its ports `(..., R, W)` and expects a
pre-reshaped tensor -- which is why `softmax` and `softmax_wide` are two kernels
instead of one.

Numeric, not structural: a reshape that got the stride wrong reads real numbers
from the wrong places and every shape check still passes.

The kernels are fixtures defined inline (rule 2b).
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims, units
from kohakutpu.lang import kernel

from kohakutpu import api
from kohakutpu import lang as L

M, N = dims("M, N")


@kernel
def subrow_sum(x=L.In(..., M, N), y=L.Out(..., M, N), *, width=128, part=8192):
    """Sum each `width`-wide SUB-ROW, broadcast back across it."""
    v, out = x.as_rows(width), y.as_rows(width)
    with units(v.parts(part)) as e:
        out[e] <<= L.row_sum(v[e])


@kernel
def subrow_max(x=L.In(..., M, N), y=L.Out(..., M, N), *, width=128, part=8192):
    v, out = x.as_rows(width), y.as_rows(width)
    with units(v.parts(part)) as e:
        out[e] <<= L.row_max(v[e])


def run(kern, x, **knobs):
    d = api.script_device("sim")
    return np.float32(np.asarray(kern(d.tensor(x), **knobs).numpy()))


def wide(rows=4, cols=512, seed=2):
    rng = np.random.default_rng(seed)
    return np.asarray(rng.normal(0, 1, (rows, cols)), np.float16)


@pytest.mark.parametrize("width", [64, 128])
def test_a_reshaped_reduction_folds_the_SUB_row(width):
    """The whole point: a 512-wide row reduces in `512/width` pieces.

    Unreshaped this is one `row_sum` over 512, which `VRED` cannot even do --
    it folds at most VLMAX lanes.
    """
    x = wide()
    got = run(subrow_sum, x, width=width)
    ref = np.float32(x).reshape(-1, width).sum(-1, keepdims=True)
    want = np.broadcast_to(ref, (ref.shape[0], width)).reshape(x.shape)
    assert np.allclose(got, want, atol=6e-2), np.abs(got - want).max()


def test_the_maximum_folds_the_same_way():
    """A different monoid over the same walk, so a stride error shows exactly."""
    x = wide()
    got = run(subrow_max, x, width=128)
    ref = np.float32(x).reshape(-1, 128).max(-1, keepdims=True)
    assert np.array_equal(
        got, np.broadcast_to(ref, (ref.shape[0], 128)).reshape(x.shape)
    )


def test_a_reshape_to_the_buffers_own_width_changes_nothing():
    """The identity case, which a stride bug would still get right -- so it is
    here to bound the claim, not to make it."""
    x = wide(rows=8, cols=128)
    assert np.allclose(
        run(subrow_sum, x, width=128),
        np.broadcast_to(np.float32(x).sum(-1, keepdims=True), x.shape),
        atol=6e-2,
    )


def test_the_view_reports_the_reshaped_extent_and_keeps_the_NAME():
    """A reshape must not become a second buffer: same name, same allocation."""
    buf = L.In(..., M, N)
    buf.name = "x"
    got = buf.as_rows(128)
    assert got.name == "x", "a reshape that renames would allocate twice"
    assert got.cols == 128
    assert got.port_shape[:1] == (Ellipsis,), "the batch axis must survive"


def test_the_row_count_is_the_element_count_over_the_width():
    """`ceildiv` stays SYMBOLIC even on two concrete ints, as `Buffer.groups`
    does, so this asserts the algebra; the numeric tests above carry the proof
    that it resolves to the right walk."""
    from kohakuaccel.lang import ceildiv

    made = L.Buffer(4, 512)
    got = made.as_rows(128)
    assert got.cols == 128
    assert repr(got.rows) == repr(ceildiv(made.elements, 128))
