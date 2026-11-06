"""One small tensor program a planner path."""

import re
from collections import Counter

import numpy as np
import pytest

from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.double import DoubleCard

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


@pytest.mark.parametrize("n,m", [(32, 64), (64, 128), (196, 128), (56, 160), (40, 64)])
def test_transposed_read(same, n, m):
    x, y = R.normal(size=(m, n)), R.normal(size=(n, m))
    same(lambda a, b: a.transpose() * 0.9 + b * 0.1, x, y)


@pytest.mark.parametrize("n,m", [(64, 128), (24, 160), (48, 208), (64, 176)])
def test_transposed_read_of_flat_rows(same, n, m):
    x, y = R.normal(size=(m, n)), R.normal(size=(n * m,))
    same(lambda a, b: a.transpose().reshape(-1) * 0.9 + b * 0.1, x, y)


@pytest.mark.parametrize("n,w", [(16, 32), (8, 64), (12, 32)])
def test_a_column_sum_of_a_product(same, n, w):
    x, y = R.normal(size=(n, w)), R.normal(size=(1, w))
    same(lambda a, b: (a * b).sum(0), x, y, tolerance=1e-2)


@pytest.mark.parametrize("k,n", [(64, 96), (128, 48), (32, 160)])
def test_a_product_of_transposed_weights(same, k, n):
    # Its own stream: the shared one's draws depend on xdist's test order.
    rng = np.random.default_rng(k * 1000 + n)
    x, w = rng.normal(size=(40, k)), rng.normal(size=(n, k), scale=k**-0.5)
    same(lambda a, m: a.dot(m.transpose()), x, w, tolerance=MX7)


@pytest.fixture
def kernels(monkeypatch):
    """Every kernel the test runs, compiled here or before, its L2 under "l2"."""
    submit_launch = DoubleCard.submit_launch
    made = []

    def kept(self, program, compiled, bindings):
        made.append({"name": compiled.name, "l2": compiled.l2_text})
        return submit_launch(self, program, compiled, bindings)

    monkeypatch.setattr(DoubleCard, "submit_launch", kept)
    return made


def test_a_column_sum_folds_its_tiles_in_registers(same, kernels):
    x, y = R.normal(size=(16, 512)), R.normal(size=(1, 512))
    same(lambda a, b: (a * b).sum(0), x, y, tolerance=1e-2)
    text = "\n".join(k["l2"] for k in kernels)
    assert "read_map" in text and "rsum" in text and "buffer" not in text


def test_a_column_mean_counts_a_repeated_row_once_a_row(same):
    x, y = _normal((8, 16, 64), (64,))
    same(lambda a, b: (a + b).mean(1), x, y, tolerance=1e-2)


def test_a_column_sum_stays_finite_where_its_padding_would_not(same):
    x = np.abs(R.normal(size=(16, 64))) + 0.5
    same(lambda a: (a * a.log2()).sum(0), x, tolerance=1e-2)


def test_a_column_max_keeps_its_negative_columns(same):
    x = R.normal(size=(16, 512)) - 8.0
    same(lambda a: a.max(0), x, tolerance=1e-2)


def test_a_column_sum_folds_two_levels_in_its_first_sweep(same, kernels):
    x, y = R.normal(size=(48, 512)), R.normal(size=(1, 512))
    same(lambda a, b: (a * b).sum(0), x, y, tolerance=1e-2)
    text = "\n".join(k["l2"] for k in kernels)
    assert "f16[12, 512]" in text and "f16[24, 512]" not in text


def test_rows_past_a_stride_are_read_where_they_lie(same, kernels):
    x, y = R.normal(size=(2, 65536)), R.normal(size=(1, 65536))
    same(lambda a, b: (a * b).sum(0), x, y, tolerance=1e-2)
    assert any("read_map" in k["l2"] for k in kernels)


def test_a_norm_wider_than_a_block_keeps_its_statistics_on_the_core(same, kernels):
    x = R.normal(size=(40, 768)) * 2.0 + 0.5
    same(lambda a: a.layernorm(eps=1e-6), x)
    assert len(kernels) == 1
    assert not re.search(r"= buffer +: \w+\[[^\]]*\] @dram", kernels[0]["l2"])


def test_a_product_lies_as_its_epilogue_reads_it(same, kernels):
    x, w, b = _normal((1, 40, 64), (96, 64), (96,))

    def projected(a, m, c):
        heads = (a.dot(m.transpose()) + c).reshape(1, 40, 3, 2, 16)
        q, k, v = (heads[:, :, i].transpose(1, 2).contiguous() for i in range(3))
        return q + k * v

    same(projected, x, w, b, tolerance=2 * MX7)
    products = [k["l2"] for k in kernels if k["name"].startswith("mm")]
    assert products and all("a: mx7a[40," in p or "out: f16[40," in p for p in products)


def test_a_product_epilogue_computes_each_subexpression_once(
    same, kernels, dev, monkeypatch
):
    dev.kernels.clear()
    dev._invalidate_eager()
    compiled = Counter()
    compile_graph = DoubleCard.compile_graph
    invocation = generated.invocation

    def counted(self, source):
        output = compile_graph(self, source)
        compiled[output["l2"]] += 1
        return output

    def canonical(device, generator, arguments):
        source, info = invocation(device, generator, arguments)
        if generator == "matmul.mm_kernel":
            ordinary = generated.graph(
                *generated._call(
                    device, generator, generated.prepared(generator, arguments)
                )
            )
            assert source == ordinary, "P=1 uses the ordinary compile/stage key"
        return source, info

    monkeypatch.setattr(DoubleCard, "compile_graph", counted)
    monkeypatch.setattr(generated, "invocation", canonical)
    x, w, b = R.normal(size=(40, 64)), R.normal(size=(96, 64)), R.normal(size=(96,))
    same(lambda a, m, c: (a.dot(m.transpose()) + c).gelu(), x, w, b, tolerance=4 * MX7)
    assert compiled and all(count == 1 for count in compiled.values())
    assert all(compiled[k["l2"]] == 1 for k in kernels)
    products = [k["l2"] for k in kernels if k["name"].startswith("mm")]
    assert products
    for text in products:
        rhs = [
            line.split("=", 1)[1].strip()
            for line in text.splitlines()
            if re.match(r"\s*e\d+ = ", line)
        ]
        assert rhs and len(rhs) == len(set(rhs)), text


def _products(kernels):
    products = [k["l2"] for k in kernels if k["name"].startswith("mm")]
    assert products
    return products


def _normal(*shapes):
    """Draws of a test's own, leaving the module's stream to the tests before."""
    g = np.random.default_rng(1)
    return [g.normal(size=s) for s in shapes]


def test_a_product_reaches_its_epilogue_through_staging_slots(same, kernels):
    x, w, b = _normal((40, 64), (96, 64), (96,))
    same(lambda a, m, c: (a.dot(m.transpose()) + c).relu(), x, w, b, tolerance=2 * MX7)
    assert all("@stage" in p for p in _products(kernels))


def test_a_bare_product_has_no_epilogue_to_overlap(same, kernels):
    x, w = _normal((40, 64), (96, 64))
    same(lambda a, m: a.dot(m.transpose()), x, w, tolerance=2 * MX7)
    assert not any("@stage" in p for p in _products(kernels))


def test_a_product_tile_fills_every_cluster_wave(same, kernels):
    # N 768 on four clusters: four tiles of 192 in one wave, not three of 256.
    x, w = _normal((40, 64), (768, 64))
    same(lambda a, m: a.dot(m.transpose()), x, w, tolerance=2 * MX7)
    assert all("mx7b[192," in p for p in _products(kernels))


def test_batched_heads_spread_over_the_clusters(same, kernels):
    p, v = _normal((8, 40, 64), (8, 64, 32))
    same(lambda a, b: (a @ b).relu(), p, v, tolerance=2 * MX7)
    assert all(re.search(r"in 0\.\.4 on mg", t) for t in _products(kernels))


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
