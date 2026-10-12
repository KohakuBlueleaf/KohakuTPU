"""tinygrad on the KTPU device (the Verilated card): programs against numpy,
the cases that once came back wrong, and refusals by name."""

import numpy as np
import pytest
from tinygrad.dtype import dtypes
from tinygrad.tensor import Tensor

import kohakutpu.application.ktpugrad as kg
from kohakutpu.language.examples import dsl as ex

RNG = np.random.default_rng(1)


def h(*s):
    return RNG.standard_normal(s).astype(np.float16)


def f(a):
    return np.asarray(a, np.float64)


def T(a):
    return Tensor(a, device="KTPU")


def I(a):
    return Tensor(np.asarray(a, np.int32), device="KTPU")


def softmax(a):
    a = f(a)
    e = np.exp(a - a.max(-1, keepdims=True))
    return e / e.sum(-1, keepdims=True)


X, Y, Z = h(32, 64), h(32, 64), h(32, 48)
W, B = (h(96, 64) / 8).astype(np.float16), h(96)
WK = (h(64, 80) / 8).astype(np.float16)  # [K, N]: x @ w
Q = h(2, 4, 8, 32)
BX, BY = h(2, 32, 64), (h(2, 64, 48) / 8).astype(np.float16)
IMG = h(2, 4, 8, 8)
LAB = RNG.integers(0, 64, 32)
INTS = RNG.integers(-200, 200, (8, 64))
N10, ODD = h(24, 10), h(3, 5, 7)
W10 = (h(10, 96) / 3).astype(np.float16)

ELEMENTWISE = {
    "add": (lambda: T(X) + T(Y), lambda: f(X) + Y),
    "affine": (lambda: T(X) * 2 + 1, lambda: f(X) * 2 + 1),
    "relu": (lambda: T(X).relu(), lambda: np.maximum(X, 0)),
    "exp": (lambda: T(X).exp(), lambda: np.exp(f(X))),
    "sqrt_abs": (lambda: T(X).abs().sqrt(), lambda: np.sqrt(np.abs(f(X)))),
    "sin": (lambda: T(X).sin(), lambda: np.sin(f(X))),
    "tanh": (lambda: T(X).tanh(), lambda: np.tanh(f(X))),
    "gelu": (
        lambda: T(X).gelu(),
        lambda: 0.5
        * f(X)
        * (1 + np.tanh(np.sqrt(2 / np.pi) * (f(X) + 0.044715 * f(X) ** 3))),
    ),
    "where": (lambda: (T(X) > T(Y)).where(T(X), T(Y)), lambda: np.where(X > Y, X, Y)),
    "bools": (
        lambda: ((T(X) > 0) & (T(Y) > 0)).where(1.0, 0.0),
        lambda: ((X > 0) & (Y > 0)) * 1.0,
    ),
    "floor_ceil": (
        lambda: T(X).floor() + T(X).ceil(),
        lambda: np.floor(f(X)) + np.ceil(f(X)),
    ),
    "f32_input": (
        lambda: Tensor(X.astype(np.float32), device="KTPU") * 3,
        lambda: f(X) * 3,
    ),
    "arange_times": (
        lambda: Tensor.arange(64).to("KTPU").cast(dtypes.half) * T(X),
        lambda: np.arange(64) * f(X),
    ),
    "row_broadcast": (lambda: T(X) + T(X[0].copy()), lambda: f(X) + X[0]),
    # Once wrong: a value a row beside one row took the narrower width.
    "one_hot": (lambda: I(LAB).one_hot(64), lambda: np.eye(64)[LAB]),
}
REDUCTIONS = {
    "sum_rows": (lambda: T(X).sum(axis=1), lambda: f(X).sum(1)),
    "max_rows": (lambda: T(X).max(axis=1), lambda: X.max(1)),
    "sum_cols": (lambda: T(X).sum(axis=0), lambda: f(X).sum(0)),
    "max_cols": (lambda: T(X).max(axis=0), lambda: X.max(0)),
    "softmax": (lambda: T(X).softmax(), lambda: softmax(X)),
    "log_softmax": (lambda: T(X).log_softmax(), lambda: np.log(softmax(X))),
    "layernorm": (
        lambda: T(X).layernorm(),
        lambda: (f(X) - f(X).mean(-1, keepdims=True))
        / np.sqrt(f(X).var(-1, keepdims=True) + 1e-5),
    ),
    "mean_of_all": (lambda: T(X).mean(), lambda: f(X).mean()),
    "std": (lambda: T(X).std(1), lambda: f(X).std(1, ddof=1)),
    # Reductions over several axes, narrow rows, statistics of statistics.
    "sum_02": (lambda: T(IMG).sum((0, 2)), lambda: f(IMG).sum((0, 2))),
    "max_13": (lambda: T(IMG).max((1, 3)), lambda: f(IMG).max((1, 3))),
    "bn_mean": (lambda: T(IMG).mean((0, 2, 3)), lambda: f(IMG).mean((0, 2, 3))),
    "bn_var": (lambda: T(IMG).var((0, 2, 3)), lambda: f(IMG).var((0, 2, 3), ddof=1)),
    "cross_entropy": (
        lambda: T(X).sparse_categorical_crossentropy(I(LAB)).reshape(1),
        lambda: np.array([-np.log(softmax(X)[np.arange(32), LAB]).mean()]),
    ),
}
INTEGER = {
    "int_add": (lambda: I(INTS) + 5, lambda: INTS + 5),
    "int_mul": (lambda: I(INTS) * 3, lambda: INTS * 3),
    "int_div": (lambda: I(INTS) // 7, lambda: INTS // 7),
    "int_mod": (lambda: I(INTS) % 7, lambda: INTS % 7),
    "argmax": (lambda: T(X).argmax(1), lambda: X.argmax(1)),
}
MOVEMENT = {
    # Once wrong: a store of loaded bytes did not wait for their fill.
    "row_slice": (lambda: T(X)[8:24], lambda: X[8:24]),
    "column_slice": (lambda: T(X)[:, 32:], lambda: X[:, 32:]),
    "row_flip": (lambda: T(X).flip(0), lambda: X[::-1]),
    "column_flip": (lambda: T(X).flip(1), lambda: X[:, ::-1]),
    "transpose": (lambda: T(X).T.contiguous(), lambda: X.T),
    "transpose_add": (lambda: T(X).T + T(Y).T, lambda: f(X).T + f(Y).T),
    "heads": (
        lambda: T(Q).permute(0, 2, 1, 3).contiguous(),
        lambda: Q.transpose(0, 2, 1, 3),
    ),
    "channels_last": (
        lambda: T(Q).permute(0, 3, 1, 2).contiguous(),
        lambda: Q.transpose(0, 3, 1, 2),
    ),
    "pad": (
        lambda: T(X).pad(((2, 2), (32, 32))),
        lambda: np.pad(f(X), ((2, 2), (32, 32))),
    ),
    "pad_value": (
        lambda: T(X).pad(((0, 0), (0, 32)), value=1.5),
        lambda: np.pad(f(X), ((0, 0), (0, 32)), constant_values=1.5),
    ),
    "cat_rows": (lambda: T(X).cat(T(Y), dim=0), lambda: np.concatenate([X, Y], 0)),
    "cat_columns": (lambda: T(X).cat(T(Z), dim=1), lambda: np.concatenate([X, Z], 1)),
    "cat_then_math": (
        lambda: T(X).cat(T(Y), dim=1) * 2 + 1,
        lambda: np.concatenate([f(X), f(Y)], 1) * 2 + 1,
    ),
    "stack": (lambda: T(X).stack(T(Y), dim=0), lambda: np.stack([X, Y], 0)),
    "broadcast_row": (
        lambda: T(X)[3:4].expand(16, 64).contiguous(),
        lambda: np.broadcast_to(X[3:4], (16, 64)),
    ),
    # Rows not of whole 32-byte words: gathered through the statistic form.
    "narrow_transpose": (lambda: T(N10).T.contiguous(), lambda: N10.T),
    "odd_permute": (
        lambda: T(ODD).permute(2, 0, 1).contiguous(),
        lambda: ODD.transpose(2, 0, 1),
    ),
    "narrow_flip": (lambda: T(ODD[0]).flip(1), lambda: ODD[0][:, ::-1]),
    "stride_slice": (lambda: T(X)[:, ::3].contiguous(), lambda: X[:, ::3]),
}
MATMUL = {
    "linear_relu": (
        lambda: (T(X) @ T(W).T + T(B)).relu(),
        lambda: np.maximum(f(X) @ f(W).T + B, 0),
    ),
    "k_major_weight": (lambda: T(X) @ T(WK), lambda: f(X) @ f(WK)),
    "transposed_left": (lambda: T(X).T @ T(Y), lambda: f(X).T @ f(Y)),
    "batched_n48": (lambda: T(BX) @ T(BY), lambda: f(BX) @ f(BY)),
    # K and N of 10 (a classifier's logits and their gradient).
    "k10": (lambda: T(N10) @ T(W10), lambda: f(N10) @ f(W10)),
    "n10": (lambda: T(N10.T.copy()).T @ T(W10), lambda: f(N10) @ f(W10)),
    "n10_out": (
        lambda: T(X) @ T(W10[:, :64].copy()).T,
        lambda: f(X) @ f(W10[:, :64]).T,
    ),
}
CASES = {**ELEMENTWISE, **REDUCTIONS, **INTEGER, **MOVEMENT, **MATMUL}
#: Exact by construction: data movement and integer math.
EXACT = (set(INTEGER) | set(MOVEMENT)) - {"transpose_add", "cat_then_math"}


@pytest.mark.parametrize("name", sorted(CASES))
def test_program_on_ktpu(dev, rel, name):
    make, ref = CASES[name]
    got, want = make().numpy(), ref()
    if name in EXACT:
        assert np.array_equal(np.asarray(got, np.float64), np.asarray(want, np.float64))
    else:
        assert rel(got, want) < 2e-2


def test_a_dsl_kernel_inside_a_tinygrad_program(dev, rel):
    x = T(X.reshape(16, 128).copy())
    y = (kg.bridge(ex.softmax)(x * 2, temperature=2.0) + 1).sum(axis=1)
    assert rel(y.numpy(), (softmax(f(X.reshape(16, 128)) * 2 / 2) + 1).sum(1)) < 2e-2


def test_assign_then_read_in_one_realize(dev):
    """The assigned value is what a later read of the same realize sees."""
    g0 = h(32, 32)
    m = Tensor.zeros(32, 32, dtype=dtypes.half, device="KTPU").contiguous().realize()
    p = T(X[:, :32].copy())
    m2 = m.assign(m * 0.5 + T(g0))
    p.assign(p.detach() - m2)
    Tensor.realize(m, p)
    assert np.array_equal(m.numpy(), g0)
    assert np.allclose(f(p.numpy()), f(X[:, :32]) - f(g0), rtol=1e-3, atol=2e-3)


def test_wide_kernels_split_at_eight_views(dev, rel):
    xs = [h(32, 64) for _ in range(12)]
    out = T(xs[0])
    for x in xs[1:]:
        out = out + T(x) * 0.5
    assert rel(out.numpy(), f(xs[0]) + sum(f(x) * 0.5 for x in xs[1:])) < 2e-2


@pytest.mark.parametrize(
    "make, why",
    [
        (lambda: T(h(1, 4, 16, 16)).conv2d(T(h(8, 4, 3, 3)), padding=1), "window"),
        (lambda: T(X).cumsum(1), "window"),
        (lambda: Tensor.rand(32, 64, device="KTPU"), "BITCAST|THREEFRY"),
    ],
)
def test_refused_by_name(dev, make, why):
    with pytest.raises((kg.KTPUUnsupported, kg.Refused), match=why):
        make().numpy()
