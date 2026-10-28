"""Planning windows without a card: tinygrad's convolution and pool kernels
(forward and backward) as plans, their mod/div indices made affine on the
gated domain (`ktpugrad.domain`) and their contractions found with their
boxes and lanes (`ktpugrad.window`). The numbers are checked on the card by
test_tinygrad.py."""

import math

import numpy as np
import pytest
from tinygrad import nn
from tinygrad.tensor import Tensor
from tinygrad.uop import Ops
from tinygrad.uop.ops import AxisType, UOp

from kohakutpu.application import ktpugrad  # noqa: F401
from kohakutpu.application.ktpugrad import domain as DOM
from kohakutpu.application.ktpugrad import lower as L
from kohakutpu.application.ktpugrad import reference as REF
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.ktpugrad import window as W
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D

RNG = np.random.default_rng(7)


@pytest.fixture(scope="module", autouse=True)
def offline():
    with D.attached(D.Device(kind="offline", board="multimesh_v9t8")):
        yield


def T(*shape):
    return Tensor(RNG.standard_normal(shape).astype(np.float16), device="KTPU")


def asts(*tensors) -> list:
    linear, _ = Tensor.linear_with_vars(*tensors)
    return [
        c.src[0]
        for c in linear.toposort()
        if c.op is Ops.CALL and c.src[0].op is Ops.SINK
    ]


def windows(plans) -> list:
    return [c for c in (W.find(p) for p in plans if p.reduce_ranges) if c is not None]


def test_a_conv_net_step_plans_every_kernel():
    """Conv -> GroupNorm -> SiLU -> avg pool -> LayerNorm, forward and
    backward: no kernel refused."""
    x, w = T(4, 3, 12, 12), T(6, 3, 5, 5)
    gn, ln = nn.GroupNorm(2, 6), nn.LayerNorm(6 * 6 * 6)
    place(gn)
    place(ln)
    y = gn(x.conv2d(w, padding=2)).silu().avg_pool2d((2, 2))
    loss = ln(y.flatten(1)).sum()
    plans = [L.plan(a) for a in asts(*loss.gradient(x, w))]
    assert plans and all(p.kind in ("const", "copy", "vector", "matmul") for p in plans)


def test_the_pool_backward_is_one_strided_read():
    """The repeat sum (`(r * 10 + y) % 11 // 2` under gates) collapses: no
    reduction is left and the gradient is read at (y // 2, x // 2)."""
    x, g = T(3, 4, 10, 10), T(3, 4, 5, 5)
    (gx,) = (x.avg_pool2d((2, 2)) * g).sum().gradient(x)
    plans = [L.plan(a) for a in asts(gx)]
    out = next(p for p in plans if p.out_shape and int(np.prod(p.out_shape)) == 1200)
    assert not out.reduce_ranges
    reads = [a for a in out.accesses if a.slot is not None]
    strides = sorted(c for a in reads for r, c in a.coef if U.extent(r) == 5)
    # g [3, 4, 5, 5]: a row of 5 and a column; the digits of 2 read neither.
    assert strides == [1, 5]
    assert all(c == 0 for a in reads for r, c in a.coef if U.extent(r) == 2)


def _conv_grads(x, w) -> list:
    """d/dx and d/dw of a convolution's sum: x's is a window over w alone."""
    return list(x.conv2d(w, padding=1).sum().gradient(x, w))


def _buffers(plans) -> dict:
    """Random flat buffers covering every slot the plans read."""
    size: dict = {}
    for p in plans:
        for a in [*p.accesses, p.a, p.b]:
            if a is None or a.slot is None:
                continue
            hi = a.off + sum(max(c, 0) * (U.extent(r) - 1) for r, c in a.coef)
            size[a.slot] = max(size.get(a.slot, 0), hi + 1)
    return {s: RNG.standard_normal(n) for s, n in size.items()}


@pytest.mark.parametrize(
    "build",
    [
        lambda: T(8, 16).cumsum(1),
        lambda: T(3, 40).cumsum(1),
        lambda: _conv_grads(T(2, 3, 8, 8), T(4, 3, 3, 3)),
    ],
    ids=["cumsum", "cumsum_odd", "conv_grad_of_sum"],
)
def test_a_window_over_one_read_is_a_sum_over_its_source(build):
    """A sum of one read times tables coupling a kept range with a reduced
    one: as a sum over the read's coordinates of the read times one table,
    the same values (`reference.evaluate`)."""
    got = build()
    plans = [L.plan(a) for a in asts(*(got if isinstance(got, list) else [got]))]
    found = [(p, W.tabled(p)) for p in plans if p.reduce_ranges]
    found = [(p, t) for p, t in found if t is not None]
    assert found
    for p, t in found:
        bufs = _buffers([p, t])
        assert len(t.reduce_ranges) and not any(a.gate for a in t.accesses)
        np.testing.assert_allclose(REF.evaluate(t, bufs), REF.evaluate(p, bufs))


@pytest.mark.parametrize(
    "stride, dilation, pad", [(2, 1, 1), (1, 2, 2), (2, 2, 3), (3, 2, 2)]
)
def test_a_strided_or_dilated_padded_conv_is_a_window(stride, dilation, pad):
    """``x[s * oy + d * ky - p]`` under its pad: one coordinate of the two
    ranges, the pad a box of it -- found as a window contraction."""
    x, w = T(2, 3, 16, 16), T(8, 3, 3, 3)
    y = x.conv2d(w, stride=stride, dilation=dilation, padding=pad)
    found = windows([L.plan(a) for a in asts(y)])
    assert len(found) == 1
    c = found[0]
    coupled = [d for s in (c.a, c.b) for d in s.dims if len(d.terms) > 1]
    g = math.gcd(stride, dilation)
    want = sorted((stride // g, dilation // g))
    # y and x: each one coordinate s * o + d * k (over their gcd).
    assert len(coupled) == 2
    assert all(sorted(abs(m) for _, m in d.terms) == want for d in coupled)
    assert any(d.box != (0, d.span) for d in coupled)


def test_a_large_image_gradient_fits_its_axes_apart():
    """A 3x3 convolution's input gradient over 512 x 512 (2.36M points
    jointly, past the domain grid): its y and x terms fitted apart, every
    kernel planned."""
    x, w = T(1, 1, 512, 512), T(1, 1, 3, 3)
    (gx,) = (x.conv2d(w, stride=2, padding=1) * T(1, 1, 256, 256)).sum().gradient(x)
    plans = [L.plan(a) for a in asts(gx)]
    assert plans


def test_a_condition_held_past_the_grid_is_the_whole_grids(monkeypatch):
    """Where a condition holds for some value of the other ranges, a block
    of them at a time past the grid's budget, is where it does whole."""
    a, b, c = (
        UOp.range(40, 0, AxisType.LOOP),
        UOp.range(30, 1, AxisType.LOOP),
        UOp.range(7, 2, AxisType.LOOP),
    )
    cond = ((a + b * 3) < 50) & ((b + c) < 9)
    whole = DOM._held(cond, [a], [b, c])
    monkeypatch.setattr(DOM, "MAX_POINTS", 64)
    blocks = DOM._held(cond, [a], [b, c])
    want = np.array(
        [
            any(i + 3 * j < 50 and j + k < 9 for j in range(30) for k in range(7))
            for i in range(40)
        ]
    )
    assert whole is not None and blocks is not None
    assert np.array_equal(whole, want) and np.array_equal(blocks, want)


@pytest.mark.parametrize("pad", [0, 2])
def test_conv_gradients_are_window_contractions(pad):
    """Both gradients plan as one sum of a product; each is found as a
    window contraction, the padding a box of the window's source."""
    x, w = T(2, 1, 12, 12), T(6, 1, 5, 5)
    oh = 12 + 2 * pad - 4
    g = T(2, 6, oh, oh)
    gx, gw = (x.conv2d(w, padding=pad) * g).sum().gradient(x, w)
    found = windows([L.plan(a) for a in asts(gx, gw)])
    assert len(found) == 2
    for c in found:
        assert c.pad % W.WORD == 0
        boxes = [d.box for s in (c.a, c.b) for d in s.dims if d.box != (0, d.span)]
        # The input's gradient is a full correlation (g padded by k - 1);
        # the weight's reads x padded by `pad`.
        is_dx = c.plan.out_n == 2 * 12 * 12
        assert bool(boxes) == (is_dx or pad > 0)
