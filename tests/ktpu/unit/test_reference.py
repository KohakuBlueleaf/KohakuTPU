"""The level-1 reference simulator, checked against numpy written out directly.

Every DSL kernel's correctness rests on this, so the assertions compare against
the mathematical definition rather than against another graph.
"""

import numpy as np
import pytest

import ktpu.dsl as D
from ktpu.interp import InterpError, run, run_one
from ktpu.ir import FP16, Graph, OpKind

RNG = np.random.default_rng(7)


def test_elementwise_and_broadcast():
    g = Graph()
    a = g.input((4, 1), FP16, name="a")
    b = g.input((3,), FP16, name="b")
    g.output(g.elementwise(OpKind.ADD, a, b))
    x, y = RNG.normal(size=(4, 1)), RNG.normal(size=(3,))
    assert np.allclose(run(g, {"a": x, "b": y})[0], x + y)


def test_matmul():
    g = Graph()
    a = g.input((8, 16), FP16, name="a")
    b = g.input((16, 4), FP16, name="b")
    g.output(g.matmul(a, b))
    x, y = RNG.normal(size=(8, 16)), RNG.normal(size=(16, 4))
    assert np.allclose(run(g, {"a": x, "b": y})[0], x @ y)


def test_reduction_keepdim_and_drop():
    g = Graph()
    x = g.input((4, 8), FP16, name="x")
    g.output(
        g.reduce(OpKind.SUM, x, (-1,), keepdim=True),
        g.reduce(OpKind.RMAX, x, (-1,)),
        g.reduce(OpKind.SUMSQ, x, (0,)),
    )
    arr = RNG.normal(size=(4, 8))
    s, mx, sq = run(g, {"x": arr})
    assert np.allclose(s, arr.sum(-1, keepdims=True))
    assert np.allclose(mx, arr.max(-1))
    assert np.allclose(sq, (arr * arr).sum(0))


def test_views():
    g = Graph()
    x = g.input((2, 6), FP16, name="x")
    g.output(
        g.reshape(x, (3, 4)),
        g.permute(x, (1, 0)),
        g.slice(x, (0, 1), (2, 4)),
        g.pad(x, ((1, 0), (0, 2))),
    )
    arr = RNG.normal(size=(2, 6))
    r, p, s, pd = run(g, {"x": arr})
    assert np.allclose(r, arr.reshape(3, 4))
    assert np.allclose(p, arr.T)
    assert np.allclose(s, arr[0:2, 1:4])
    assert np.allclose(pd, np.pad(arr, ((1, 0), (0, 2))))


def test_select_uses_a_mask_as_a_value():
    g = Graph()
    x = g.input((6,), FP16, name="x")
    z = g.const(0.0, FP16)
    mask = g.elementwise(OpKind.CMPLT, z, x)
    g.output(g.elementwise(OpKind.SELECT, mask, x, z))
    arr = RNG.normal(size=(6,))
    assert np.allclose(run(g, {"x": arr})[0], np.maximum(arr, 0.0))


def test_missing_input_names_what_is_available():
    g = Graph()
    g.output(g.input((2,), FP16, name="x"))
    with pytest.raises(InterpError, match="missing input"):
        run(g, {"y": np.zeros(2)})


def test_shape_mismatch_is_caught():
    g = Graph()
    g.output(g.input((2, 3), FP16, name="x"))
    with pytest.raises(InterpError, match="graph says"):
        run(g, {"x": np.zeros((3, 2))})


# ---------------------------------------------------------------------------
# The DSL library, against its mathematical definition
# ---------------------------------------------------------------------------

S = D.TensorSpec((16, 64), FP16)
V = D.TensorSpec((64,), FP16)


def test_rmsnorm_matches_its_definition():
    x = RNG.normal(size=(16, 64))
    gam = RNG.normal(size=(64,))
    got = run_one(D.trace(D.rmsnorm, S, V, eps=1e-5), x, gam)
    want = x / np.sqrt((x * x).mean(-1, keepdims=True) + 1e-5) * gam
    assert np.allclose(got, want, rtol=1e-9)


def test_softmax_matches_its_definition_and_survives_big_logits():
    for scale in (1.0, 400.0):
        x = RNG.normal(size=(16, 64)) * scale
        got = run_one(D.trace(D.softmax, S), x)
        e = np.exp(x - x.max(-1, keepdims=True))
        assert np.allclose(got, e / e.sum(-1, keepdims=True), rtol=1e-9)
        assert np.isfinite(got).all()


def test_sigmoid_and_silu():
    x = RNG.normal(size=(16, 64))
    want = 1.0 / (1.0 + np.exp(-x))
    assert np.allclose(run_one(D.trace(D.sigmoid, S), x), want, rtol=1e-9)
    assert np.allclose(run_one(D.trace(D.silu, S), x), x * want, rtol=1e-9)


def test_gelu_tanh_form_tracks_the_exact_erf_form():
    from math import erf

    x = np.linspace(-4, 4, 16 * 64).reshape(16, 64)
    got = run_one(D.trace(D.gelu, S), x)
    exact = x * 0.5 * (1.0 + np.vectorize(erf)(x / np.sqrt(2.0)))
    assert np.abs(got - exact).max() < 2e-3
