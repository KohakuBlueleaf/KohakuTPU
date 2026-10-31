"""One small tensor program a planner path."""

import numpy as np
import pytest

#: A contraction reads its operands quantised to seven bits a block.
MX7 = 0.02

R = np.random.default_rng(0)
A = R.normal(size=(16, 32))
B = R.normal(size=(32, 32))
C = R.normal(size=(16, 32))
P = np.abs(A) + 0.25

ELEMENTWISE = {
    "add": lambda a, c: a + c,
    "sub": lambda a, c: a - c,
    "mul": lambda a, c: a * c,
    "where": lambda a, c: (a > c).where(a, c),
    "max": lambda a, c: a.maximum(c),
    "fused": lambda a, c: (a * 2.0 + c).relu() * a - c,
}
UNARY = {
    "exp": lambda a: a.exp(),
    "relu": lambda a: a.relu(),
    "gelu": lambda a: a.gelu(),
    "silu": lambda a: a.silu(),
    "neg": lambda a: -a,
    "abs": lambda a: a.abs(),
}
POSITIVE = {
    "log": lambda a: a.log(),
    "sqrt": lambda a: a.sqrt(),
    "reciprocal": lambda a: a.reciprocal(),
}
REDUCE = {
    "sum": lambda a: a.sum(),
    "sum_rows": lambda a: a.sum(1),
    "sum_columns": lambda a: a.sum(0),
    "mean": lambda a: a.mean(1),
    "max": lambda a: a.max(1),
    "softmax": lambda a: a.softmax(1),
    "log_softmax": lambda a: a.log_softmax(1),
}
SHAPE = {
    "reshape": lambda a: a.reshape(32, 16) + 1.0,
    "transpose": lambda a: a.transpose() + 1.0,
    "shrink": lambda a: a[2:10, 4:20] + 1.0,
    "pad": lambda a: a.pad(((1, 1), (2, 2))) + 1.0,
    "flip": lambda a: a.flip(1) + 1.0,
    "expand": lambda a: a[:, :1].expand(16, 32) + a,
}


@pytest.mark.parametrize("name", ELEMENTWISE)
def test_elementwise(same, name):
    same(ELEMENTWISE[name], A, C)


@pytest.mark.parametrize("name", UNARY)
def test_unary(same, name):
    same(UNARY[name], A)


@pytest.mark.parametrize("name", POSITIVE)
def test_positive(same, name):
    same(POSITIVE[name], P)


@pytest.mark.parametrize("name", REDUCE)
def test_reduce(same, name):
    same(REDUCE[name], A)


@pytest.mark.parametrize("name", SHAPE)
def test_shape(same, name):
    same(SHAPE[name], A)


def test_matmul(same):
    same(lambda a, b: a @ b, A, B, tolerance=MX7)


def test_matmul_then_activation(same):
    same(lambda a, b: (a @ b).relu().sum(1), A, B, tolerance=MX7)


def test_conv(same):
    x = R.normal(size=(2, 1, 12, 12))
    w = R.normal(size=(4, 1, 3, 3))
    same(lambda x, w: x.conv2d(w, padding=1), x, w, tolerance=MX7)


def test_max_pool(same):
    same(lambda x: x.max_pool2d((2, 2)), R.normal(size=(2, 4, 8, 8)))


def test_variance(same):
    same(lambda a: (a.mean(1, keepdim=True) - a).square().mean(), A)
