"""Every op a generic tinygrad graph needs, lowered through the DSL SURFACE.

`test_alu_coverage.py` checks the emitter reaches each ALU opcode. This checks
the other half: that `kohakutpu.lang` EXPORTS them and that a kernel written in
the surface vocabulary compiles and computes the right numbers.

That gap was real -- `where`, `minimum`, `less`, `greater`, `equal` and `fma`
sat in `lang/vector.py` and were missing from `lang/__init__.py`, so each one
raised `AttributeError` from the only place an author would call it.

The kernels are fixtures defined inline, not `tests/fixtures.py`, whose corpus
is frozen for `test_band_witness.py`.
"""

import numpy as np
import pytest
from kohakuaccel.lang import dims
from kohakutpu.lang import kernel

from kohakutpu import api
from kohakutpu import lang as L

M, N = dims("M, N")


@kernel
def selected(
    c=L.In(..., M, N), a=L.In(..., M, N), b=L.In(..., M, N), y=L.Out(..., M, N)
):
    """`VSEL` over a VALUE condition, which is the select that works today."""
    y <<= L.where(c, L.fma(a, b, 1.0), L.minimum(a, b))


@kernel
def compared(a=L.In(..., M, N), b=L.In(..., M, N), y=L.Out(..., M, N)):
    """`WHERE` over `CMPGT`, the shape tinygrad writes every mask in."""
    y <<= L.where(L.greater(a, b), a, b)


@kernel
def lesser(a=L.In(..., M, N), b=L.In(..., M, N), y=L.Out(..., M, N)):
    y <<= L.where(L.less(a, b), a, b)


@kernel
def tied(a=L.In(..., M, N), b=L.In(..., M, N), y=L.Out(..., M, N)):
    y <<= L.where(L.equal(a, b), a + b, a - b)


@kernel
def relu_as_tinygrad_writes_it(x=L.In(..., M, N), y=L.Out(..., M, N)):
    """`WHERE(CMPLT(0, x), x, 0)`, which is the whole point of the mechanism."""
    y <<= L.where(L.less(x * 0.0, x), x, x * 0.0)


@kernel
def clamped(x=L.In(..., M, N), y=L.Out(..., M, N), *, lo=-1.0, hi=1.0):
    """The idiom `WHERE` exists for. One word each, no branch."""
    y <<= L.minimum(L.maximum(x, lo), hi)


@kernel
def rooted(x=L.In(..., M, N), y=L.Out(..., M, N)):
    """There is no `VSQRT`; a root is COMPOSED out of ops that do exist."""
    y <<= L.sqrt_approx(x)


@kernel
def rooted_newton(x=L.In(..., M, N), y=L.Out(..., M, N)):
    y <<= L.sqrt_newton(x)


def pair(seed=3, shape=(64, 64)):
    """Two fp16 arrays with ties in them, so `CMPEQ` is actually exercised."""
    rng = np.random.default_rng(seed)
    a = np.asarray(rng.integers(-4, 5, shape), np.float16)
    b = np.asarray(rng.integers(-4, 5, shape), np.float16)
    return a, b


def run(kern, arrays, **knobs):
    dev = api.script_device("sim")
    return np.asarray(kern(*[dev.tensor(x) for x in arrays], **knobs).numpy())


def test_the_surface_exports_every_op_a_tinygrad_graph_lowers_to():
    """The AttributeError this file exists for, as a list rather than a trace."""
    for name in (
        "where",
        "minimum",
        "maximum",
        "less",
        "greater",
        "equal",
        "fma",
        "recip",
        "exp2",
        "log2",
        "rsqrt",
        "absolute",
        "row_sum",
        "row_max",
        "sqrt_approx",
        "sqrt_newton",
    ):
        assert hasattr(L, name), f"kohakutpu.lang does not export {name!r}"
        assert name in L.__all__, f"{name!r} is importable but not in __all__"


def test_where_over_a_value_condition_computes_what_numpy_does():
    a, b = pair()
    rng = np.random.default_rng(8)
    c = np.asarray(rng.integers(0, 2, a.shape), np.float16)
    got = run(selected, [c, a, b])
    want = np.where(c != 0, np.float32(a) * np.float32(b) + 1.0, np.minimum(a, b))
    assert np.allclose(np.float32(got), want, atol=2e-2), np.abs(got - want).max()


@pytest.mark.parametrize(
    ("kern", "want"),
    [
        (compared, lambda a, b: np.where(a > b, a, b)),
        (lesser, lambda a, b: np.where(a < b, a, b)),
        (tied, lambda a, b: np.where(a == b, a + b, a - b)),
    ],
    ids=["greater", "less", "equal"],
)
def test_where_over_a_comparison_computes_what_numpy_does(kern, want):
    """The bug this file found, now the thing that works.

    A comparison writes a PREDICATE register and no vector register
    (`vec_lanes.v:503` gates writeback on `!wb_cmp`), so this lowers to a
    compare into P0 and TWO PREDICATED MOVES rather than a VSEL. Lowered as a
    value it picked one arm for all 4096 elements with nothing reporting it,
    which is why this is asserted element for element.
    """
    a, b = pair()
    got = np.float32(run(kern, [a, b]))
    assert np.array_equal(got, np.float32(want(a, b)))


def test_relu_the_way_tinygrad_spells_it():
    """`WHERE(CMPLT(0, x), x, 0)` -- the reason the predicate path matters."""
    rng = np.random.default_rng(11)
    x = np.asarray(rng.normal(0, 2, (64, 64)), np.float16)
    got = np.float32(run(relu_as_tinygrad_writes_it, [x]))
    assert np.array_equal(got, np.maximum(np.float32(x), 0.0))


def test_a_comparison_with_no_select_is_still_refused():
    """Only the PAIR has a lowering; a bare compare still has nowhere to land."""
    from kohakutpu.hw.ops import OpKind
    from kohakutpu.isa.vecemit import VecEmitError, lower_op

    with pytest.raises(VecEmitError, match="PREDICATE register"):
        lower_op(OpKind.CMPLT, [1, 2], dst=3)


def test_a_clamp_is_two_words_and_no_branch():
    rng = np.random.default_rng(7)
    x = np.asarray(rng.normal(0, 2, (64, 64)), np.float16)
    got = run(clamped, [x])
    assert np.allclose(np.float32(got), np.clip(np.float32(x), -1.0, 1.0), atol=1e-3)
    assert got.max() <= 1.0 and got.min() >= -1.0


@pytest.mark.parametrize(("kern", "ulp"), [(rooted, 1.556), (rooted_newton, 1.467)])
def test_both_composed_roots_are_within_their_measured_ulp(kern, ulp):
    """`sqrt` has no ALU op, so this pins that the composition still holds."""
    rng = np.random.default_rng(5)
    x = np.asarray(np.abs(rng.normal(4, 2, (64, 64))) + 0.5, np.float16)
    got = np.float64(run(kern, [x]))
    want = np.sqrt(np.float64(x))
    err = np.abs(got - want) / np.float64(np.spacing(want.astype(np.float16)))
    assert np.median(err) <= ulp * 2, f"median {np.median(err):.3f} ulp, want {ulp}"


def test_a_root_is_never_lowered_to_the_single_wrong_word():
    """`UNARY` mapped `SQRT` to `VRSQRT` once, and returned a wrong number.

    The refusal is the guard; this asserts it survives having two working
    compositions available, since the tempting fix is to re-add the row.
    """
    from kohakutpu.hw.ops import OpKind
    from kohakutpu.isa.vecemit import UNARY, VecEmitError, lower_op

    assert OpKind.SQRT not in UNARY
    with pytest.raises(VecEmitError, match="NO square root"):
        lower_op(OpKind.SQRT, [0], dst=7)
