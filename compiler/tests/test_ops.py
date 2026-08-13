"""`ops/`: one kernel per tensor-level operator, each checked against numpy.

The OPS layer, not the compiler -- so importing `kohakutpu.ops` is the point
here, where a compiler test would define its own fixtures instead (rule 2b).

What this pins beyond the arithmetic: every op a tinygrad backend must
implement is PRESENT, and each is ONE pass. An op that quietly became two
stages is a kernel wearing an op's name, and the caller who strung four of them
together is then paying eight passes.
"""

import numpy as np
import pytest

from kohakutpu import api, ops

M, N = 64, 64


def dev():
    return api.script_device("sim")


def arr(shape=(M, N), seed=3, scale=1.0):
    rng = np.random.default_rng(seed)
    return np.asarray(rng.normal(0, scale, shape), np.float16)


def run(kern, arrays, **knobs):
    d = dev()
    return np.float32(np.asarray(kern(*[d.tensor(a) for a in arrays], **knobs).numpy()))


#: op -> (operands, the float32 reference). One shape each; these are the
#: tinygrad table, so a missing entry is a backend that cannot lower a graph.
UNARY = [
    ("neg", ops.neg, lambda x: -x),
    ("absolute", ops.absolute, np.abs),
    ("exp2", ops.exp2, np.exp2),
    ("recip", ops.recip, lambda x: 1.0 / x),
    ("relu", ops.relu, lambda x: np.maximum(x, 0.0)),
    ("silu", ops.silu, lambda x: x / (1.0 + np.exp(-x))),
]

BINARY = [
    ("sub", ops.sub, lambda a, b: a - b),
    ("mul", ops.mul, lambda a, b: a * b),
    ("residual", ops.residual, lambda a, b: a + b),
    ("maximum", ops.maximum, np.maximum),
    ("minimum", ops.minimum, np.minimum),
]


@pytest.mark.parametrize(("name", "kern", "want"), UNARY, ids=[r[0] for r in UNARY])
def test_a_unary_op_computes_what_numpy_does(name, kern, want):
    x = arr()
    got = run(kern, [x])
    ref = want(np.float32(x))
    assert np.allclose(got, ref, atol=6e-3, rtol=6e-3), np.abs(got - ref).max()


@pytest.mark.parametrize(("name", "kern", "want"), BINARY, ids=[r[0] for r in BINARY])
def test_a_binary_op_computes_what_numpy_does(name, kern, want):
    a, b = arr(seed=1), arr(seed=2)
    got = run(kern, [a, b])
    assert np.allclose(got, want(np.float32(a), np.float32(b)), atol=6e-3)


def test_log2_and_rsqrt_over_positive_input():
    """Separated because both are undefined on the negatives the others use."""
    x = np.asarray(np.abs(arr(seed=4)) + 0.5, np.float16)
    assert np.allclose(run(ops.log2, [x]), np.log2(np.float32(x)), atol=6e-3)
    got = run(ops.rsqrt, [x])
    ref = 1.0 / np.sqrt(np.float32(x))
    assert np.abs(got - ref).max() / np.abs(ref).max() < 5e-3


def test_div_is_the_reciprocal_then_a_multiply():
    a, b = arr(seed=1), np.asarray(np.abs(arr(seed=2)) + 0.5, np.float16)
    got = run(ops.div, [a, b])
    ref = np.float32(a) / np.float32(b)
    assert np.abs(got - ref).max() / np.abs(ref).max() < 1e-2


def test_where_selects_on_a_value_condition():
    a, b = arr(seed=1), arr(seed=2)
    rng = np.random.default_rng(6)
    c = np.asarray(rng.integers(0, 2, (M, N)), np.float16)
    got = run(ops.where, [c, a, b])
    assert np.array_equal(
        got, np.where(np.float32(c) != 0, np.float32(a), np.float32(b))
    )


def test_the_reductions_broadcast_back_across_the_row():
    x = arr()
    xf = np.float32(x)
    assert np.allclose(run(ops.row_sum, [x]), xf.sum(-1, keepdims=True), atol=6e-2)
    assert np.array_equal(
        run(ops.row_max, [x]), np.broadcast_to(xf.max(-1, keepdims=True), xf.shape)
    )


def test_matmul_against_float64():
    a, b = arr((32, 64), seed=1, scale=0.2), arr((48, 64), seed=2, scale=0.2)
    got = run(ops.matmul, [a, b])
    ref = np.float64(a) @ np.float64(b).T
    assert np.abs(got - ref).max() / np.abs(ref).max() < 5e-2


def test_softmax_rows_sum_to_one():
    x = arr()
    got = run(ops.softmax, [x])
    assert np.allclose(got.sum(-1), 1.0, atol=1e-2)
    xf = np.float64(x)
    ref = np.exp(xf - xf.max(-1, keepdims=True))
    ref /= ref.sum(-1, keepdims=True)
    assert np.abs(got - ref).max() < 5e-3


@pytest.mark.parametrize("kern", [ops.rmsnorm, ops.layernorm], ids=["rms", "layer"])
def test_a_norm_carries_NO_affine(kern):
    """Two operands would mean the weight folded in, which makes it a kernel."""
    assert [p for p in kern.signature.ports if p.role == "in"].__len__() == 1


def test_layernorm_and_rmsnorm_against_float64():
    x = arr(scale=2.0)
    xf = np.float64(x)
    mu = xf.mean(-1, keepdims=True)
    var = ((xf - mu) ** 2).mean(-1, keepdims=True)
    want = (xf - mu) / np.sqrt(var + 1e-5)
    got = run(ops.layernorm, [x])
    assert np.abs(got - want).max() / np.abs(want).max() < 5e-3

    want = xf / np.sqrt((xf**2).mean(-1, keepdims=True) + 1e-5)
    got = run(ops.rmsnorm, [x])
    assert np.abs(got - want).max() / np.abs(want).max() < 5e-3


ONE_PASS = [r[1] for r in UNARY] + [r[1] for r in BINARY] + [ops.softmax]


@pytest.mark.parametrize("kern", ONE_PASS, ids=lambda k: k.name)
def test_an_op_is_ONE_pass(kern):
    """The property that separates `ops/` from `kernels/`, asserted not assumed.

    An op that became two stages costs a caller a whole extra trip through
    memory, and nothing about calling it says so.
    """
    d = dev()
    nin = len([p for p in kern.signature.ports if p.role == "in"])
    args = [d.tensor(arr(seed=i + 1)) for i in range(nin)]
    assert len(kern.plan(*args).stages) == 1, kern.name


def test_relu_is_one_word_now_that_VMAX_is_demonstrated():
    """It was `(x + |x|) * 0.5` -- three words -- while the slot was a guess."""
    d = dev()
    got = ops.relu.plan(d.tensor(arr()))
    stmts = [s for st in got.stages for s in st.instances[0].stmts]
    assert len([s for s in stmts if s.kind == "apply"]) == 1
