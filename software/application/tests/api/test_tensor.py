"""The tensor API on the Verilated card: each op against numpy, and how many
kernels a fused chain runs."""

import numpy as np
import pytest

from kohakutpu.application import api as kp
from kohakutpu.language.examples import dsl as ex
from kohakutpu.language.numerics.mxfp7 import value_fp16

RNG = np.random.default_rng(7)


def h(*s):
    return RNG.standard_normal(s).astype(np.float16)


X, Y, B = h(64, 128), h(64, 128), h(128)
XW = h(32, 512)
G = (h(128) * 0.5 + 1).astype(np.float16)
W = (h(256, 128) / 11).astype(np.float16)
BIAS = h(256)


def f(a):
    return np.asarray(a, np.float64)


def softmax(a):
    a = f(a)
    e = np.exp(a - a.max(-1, keepdims=True))
    return e / e.sum(-1, keepdims=True)


def layernorm(a, g=None, b=None):
    a = f(a)
    d = a - a.mean(-1, keepdims=True)
    r = d / np.sqrt((d * d).mean(-1, keepdims=True) + 1e-5)
    return r * (1 if g is None else f(g)) + (0 if b is None else f(b))


def gelu(v):
    return 0.5 * v * (1 + np.tanh(np.sqrt(2 / np.pi) * (v + 0.044715 * v**3)))


T = kp.tensor
#: name -> (api, numpy reference, kernels it runs when fused)
CASES = {
    "chain": (
        lambda: (T(X) * 2 + T(Y)).relu() - 1,
        lambda: np.maximum(f(X) * 2 + Y, 0) - 1,
        1,
    ),
    "row_broadcast": (lambda: T(X) + T(B), lambda: f(X) + B, 1),
    "row_sums": (lambda: T(X).sum(-1), lambda: f(X).sum(-1), 1),
    "mean_of_all": (lambda: T(X).mean(), lambda: f(X).mean(), None),
    "softmax": (lambda: T(X).softmax(), lambda: softmax(X), 1),
    "softmax_wide": (lambda: T(XW).softmax(), lambda: softmax(XW), None),
    "layernorm": (lambda: T(X).layernorm(T(G), T(B)), lambda: layernorm(X, G, B), 1),
    "layernorm_wide": (lambda: T(XW).layernorm(), lambda: layernorm(XW), None),
    "std": (lambda: T(X).std(-1, keepdim=True), lambda: f(X).std(-1, keepdims=True), 1),
    "where": (
        lambda: kp.where(T(X) > T(Y), T(X), 0.0),
        lambda: np.where(X > Y, f(X), 0),
        1,
    ),
    "gelu": (lambda: T(X).gelu(), lambda: gelu(f(X)), 1),
    "linear_gelu": (
        lambda: kp.linear(T(X), T(W), T(BIAS)).gelu(),
        lambda: gelu(value_fp16(X) @ value_fp16(W).T + BIAS),
        None,
    ),
    "odd_width": (lambda: T(X[:, :77].copy()) * 3, lambda: f(X[:, :77]) * 3, 1),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_op_on_the_card(dev, rel, name):
    make, ref, kernels = CASES[name]
    n0 = len(dev.runs)
    got = make().numpy()
    assert rel(got, ref()) < 2e-2
    if kernels is not None:
        assert len(dev.runs) - n0 == kernels


def test_a_product_and_its_epilogue_are_one_matmul_kernel(dev):
    n0 = len(dev.runs)
    kp.linear(T(X), T(W), T(BIAS)).gelu().numpy()
    kinds = [r.kind for r in dev.runs[n0:]]
    assert kinds.count("matmul") == 1


def test_without_fusion_every_op_is_a_kernel(dev, rel):
    old = kp.set_fusion(False)
    try:
        n0 = len(dev.runs)
        got = ((T(X) * 2 + T(Y)).relu() - 1).numpy()
        assert len(dev.runs) - n0 == 4
        assert rel(got, np.maximum(f(X) * 2 + Y, 0) - 1) < 2e-2
    finally:
        kp.set_fusion(old)


def test_your_own_kernel_on_tensors(dev, rel):
    y = kp.function(ex.softmax)(T(X), temperature=2.0)
    got = (y * 10).sum(-1).numpy()
    assert rel(got, (softmax(f(X) / 2) * 10).sum(-1)) < 2e-2


@pytest.mark.parametrize(
    "make, why",
    [
        (lambda: T(X) @ T(W), "store the weight"),
        (lambda: T(X).sum(0), "rows are the last axis"),
        # [64, 64] + [64]: a row statistic broadcast as a row of values.
        (lambda: T(X[:, :64].copy()) + T(X[:, :64].copy()).sum(-1), "keepdim=True"),
    ],
)
def test_refused_by_name(dev, make, why):
    with pytest.raises(kp.Unsupported, match=why):
        make().numpy()
