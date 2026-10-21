"""tinygrad on the KTPU device (the Verilated card): programs against numpy,
the cases that once came back wrong, and refusals by name."""

import numpy as np
import pytest
from numpy.lib.stride_tricks import sliding_window_view
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
#: One-hot float labels: the loss reads labels as values.
ONEHOT = np.eye(64, dtype=np.float16)[LAB]
N10, ODD = h(24, 10), h(3, 5, 7)
POS = h(32, 64)
W10 = (h(10, 96) / 3).astype(np.float16)
WIDE, WIDE2 = h(8, 1184), h(8, 2400)
W84 = (h(84, 64) / 8).astype(np.float16)
B120, CH, H84 = h(8, 120), h(8, 6, 28, 28), h(32, 84)
FAR, FARTHER, CH64 = h(4, 20480), h(2, 40960), h(64, 6, 28, 28)
APART = h(64, 6144)
NHWC16 = h(2, 64, 16)

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
        lambda: Tensor.arange(64, dtype=dtypes.half).to("KTPU") * T(X),
        lambda: np.arange(64) * f(X),
    ),
    "row_broadcast": (lambda: T(X) + T(X[0].copy()), lambda: f(X) + X[0]),
    # Once a loop: a [T, D] table across the batch has no row view even laid out.
    "batch_table": (
        lambda: T(BX) + T(POS) + T(BX[0, 0].copy()),
        lambda: f(BX) + POS + BX[0, 0],
    ),
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
    # Rows of 32 x an odd number: column blocks of 32, their partials folded
    # by halving (a group norm's statistic over 3 x 28 x 28 padded).
    "sum_wide_rows": (lambda: T(WIDE).sum(axis=1), lambda: f(WIDE).sum(1)),
    "max_wide_rows": (lambda: T(WIDE2).max(axis=1), lambda: f(WIDE2).max(1)),
    # Down rows of 120 (a bias gradient): laid out 128 apart exactly.
    "sum_cols_120": (lambda: T(B120).sum(axis=0), lambda: f(B120).sum(0)),
    # A channel's sum over batch and image: rows too far apart for one walk.
    "channel_sum": (lambda: T(CH).sum((0, 2, 3)), lambda: f(CH).sum((0, 2, 3))),
    # Rows too far apart for a band's step (ainc): the step pass runs the
    # stream's trips as a node loop, one trip a run.
    "sum_far_rows": (lambda: T(FAR).sum(axis=1), lambda: f(FAR).sum(1)),
    "max_far_rows": (lambda: T(FAR).max(axis=1), lambda: f(FAR).max(1)),
    "mean_far_rows": (lambda: T(FAR).mean(axis=1), lambda: f(FAR).mean(1)),
    # Rows 12 KB apart, several trips a core.
    "sum_rows_apart": (lambda: T(APART).sum(axis=1), lambda: f(APART).sum(1)),
    "sum_farther_rows": (lambda: T(FARTHER).sum(axis=1), lambda: f(FARTHER).sum(1)),
    "channel_sum_64": (
        lambda: T(CH64).sum((0, 2, 3)),
        lambda: f(CH64).sum((0, 2, 3)),
    ),
    "std": (lambda: T(X).std(1), lambda: f(X).std(1, ddof=1)),
    # Reductions over several axes, narrow rows, statistics of statistics.
    "sum_02": (lambda: T(IMG).sum((0, 2)), lambda: f(IMG).sum((0, 2))),
    "max_13": (lambda: T(IMG).max((1, 3)), lambda: f(IMG).max((1, 3))),
    "bn_mean": (lambda: T(IMG).mean((0, 2, 3)), lambda: f(IMG).mean((0, 2, 3))),
    "bn_var": (lambda: T(IMG).var((0, 2, 3)), lambda: f(IMG).var((0, 2, 3), ddof=1)),
    "cross_entropy": (
        lambda: -(T(X).log_softmax() * T(ONEHOT)).sum(-1).mean().reshape(1),
        lambda: np.array([-np.log(softmax(X)[np.arange(32), LAB]).mean()]),
    ),
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
    # Six channels of rows of 16 innermost to first (a convolution's product
    # to NCHW): the 4 x 4 lane transpose over 16 lanes, the rest dropped.
    "channels_first_6": (
        lambda: T(NHWC16)[:, :, :6].permute(0, 2, 1).contiguous(),
        lambda: NHWC16[:, :, :6].transpose(0, 2, 1),
    ),
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
    # A product of 84 columns (held as padded rows) read by another's
    # epilogue: made row-major through the statistic form.
    "n84_epilogue": (
        lambda: T(X) @ T(W84).T + (T(Y) @ T(W84).T).relu(),
        lambda: f(X) @ f(W84).T + np.maximum(f(Y) @ f(W84).T, 0),
    ),
    # A dense [32, 84] operand of the epilogue: laid out 96 apart exactly.
    "n84_dense_operand": (
        lambda: T(X) @ T(W84).T * T(H84),
        lambda: f(X) @ f(W84).T * f(H84),
    ),
}


def conv(x, w, pad):
    """f64 cross-correlation of [B, C, H, W] with [O, C, KH, KW], zero padding."""
    xp = np.pad(f(x), ((0, 0), (0, 0), (pad, pad), (pad, pad)))
    p = sliding_window_view(xp, w.shape[2:], axis=(2, 3))
    return np.einsum("bchwij,ocij->bohw", p, f(w))


def conv_grads(x, w, pad, g):
    """d sum(conv(x, w) * g) / dx and / dw."""
    xp = np.pad(f(x), ((0, 0), (0, 0), (pad, pad), (pad, pad)))
    kh, kw = w.shape[2:]
    dw = np.einsum(
        "bchwij,bohw->ocij", sliding_window_view(xp, (kh, kw), axis=(2, 3)), f(g)
    )
    dxp = np.zeros_like(xp)
    oh, ow = g.shape[2:]
    for i in range(kh):
        for j in range(kw):
            dxp[:, :, i : i + oh, j : j + ow] += np.einsum(
                "bohw,oc->bchw", f(g), f(w[:, :, i, j])
            )
    return dxp[:, :, pad : pad + x.shape[2], pad : pad + x.shape[3]], dw


CX, CW = h(2, 3, 6, 6), (h(4, 3, 3, 3) / 3).astype(np.float16)
C1, CW1 = h(2, 1, 8, 8), (h(4, 1, 5, 5) / 5).astype(np.float16)
WINDOW = {
    "conv2d": (lambda: T(CX).conv2d(T(CW)), lambda: conv(CX, CW, 0)),
    "conv2d_padded": (lambda: T(CX).conv2d(T(CW), padding=1), lambda: conv(CX, CW, 1)),
    # One input channel: no channel to lay innermost, a lane of one.
    "conv2d_one_channel": (
        lambda: T(C1).conv2d(T(CW1), padding=2),
        lambda: conv(C1, CW1, 2),
    ),
    "avg_pool2d": (
        lambda: T(IMG).avg_pool2d((2, 2)),
        lambda: f(IMG).reshape(2, 4, 4, 2, 4, 2).mean((3, 5)),
    ),
}
CASES = {**ELEMENTWISE, **REDUCTIONS, **MOVEMENT, **MATMUL, **WINDOW}
#: Exact by construction: data movement.
EXACT = set(MOVEMENT) - {"transpose_add", "cat_then_math"}


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
    "x, w, pad", [(CX, CW, 0), (C1, CW1, 2)], ids=["plain", "padded"]
)
def test_conv_gradients(dev, rel, x, w, pad):
    """Both gradients of a convolution: the input's (a window with a
    negative stride on the kernel) and the weight's (a window summed over
    the batch)."""
    oh = x.shape[2] + 2 * pad - w.shape[2] + 1
    g = h(x.shape[0], w.shape[0], oh, oh)
    tx, tw = T(x), T(w)
    gx, gw = (tx.conv2d(tw, padding=pad) * T(g)).sum().gradient(tx, tw)
    dx, dw = conv_grads(x, w, pad, g)
    assert rel(gx.numpy(), dx) < 2e-2
    assert rel(gw.numpy(), dw) < 2e-2


def test_avg_pool_gradient(dev, rel):
    """The backward of tinygrad's pool (a repeat sliced into windows): each
    input element in exactly one window; through a SiLU, the upsampled
    gradient times the SiLU's derivative (a read broadcast over the window's
    digits, laid out over the output first)."""
    g = h(2, 4, 4, 4)
    up = np.repeat(np.repeat(f(g), 2, 2), 2, 3) / 4
    tx = T(IMG)
    (gx,) = (tx.avg_pool2d((2, 2)) * T(g)).sum().gradient(tx)
    assert rel(gx.numpy(), up) < 2e-2
    (gs,) = (tx.silu().avg_pool2d((2, 2)) * T(g)).sum().gradient(tx)
    s = 1 / (1 + np.exp(-f(IMG)))
    assert rel(gs.numpy(), up * (s + f(IMG) * s * (1 - s))) < 2e-2


@pytest.mark.parametrize(
    "make, why",
    [
        (lambda: T(X).cumsum(1), "window"),
        (lambda: Tensor.rand(32, 64, device="KTPU"), "BITCAST|THREEFRY"),
    ],
)
def test_refused_by_name(dev, make, why):
    with pytest.raises((kg.KTPUUnsupported, kg.Refused), match=why):
        make().numpy()
