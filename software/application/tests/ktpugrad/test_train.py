"""Training on the KTPU device: `place` and `Adam16` against an independent
witness, and one backward step of a module."""

import math

import numpy as np
import pytest
from examples.tinygrad.mnist_mlp import train_step
from examples.tinygrad.mnist_models import MLP
from tinygrad import nn
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters, get_state_dict
from tinygrad.tensor import Tensor

from kohakutpu.application.ktpugrad.nn import Adam16, place

RNG = np.random.default_rng(3)
LR, S, STEPS = 1e-4, 1024.0, 3


def adam_steps(w0, grads, compensate: bool) -> tuple:
    """`Adam16` on KTPU over `grads` (already scaled): the weights and what
    the error buffer holds (zero without compensation)."""
    p = Tensor(w0, device="KTPU")
    opt = Adam16([p], lr=LR, loss_scale=S, compensate=compensate)
    with Context(TRAINING=1):
        for g in grads:
            p.grad = Tensor(g, device="KTPU")
            opt.step()
    err = opt.err[0].numpy().astype(np.float64) if compensate else 0.0
    return p.numpy().astype(np.float64), err


def exact_path(w0, grads, b1=0.9, b2=0.95, eps=1e-8):
    """The same updates (m, rms rounded to f16 as stored) never rounded into
    the weight: what a compensated f16 weight must track."""
    w = w0.astype(np.float64)
    m = r = np.zeros_like(w0)
    for t, g in enumerate(grads, 1):
        g = g.astype(np.float64)
        m = (b1 * m.astype(np.float64) + (1 - b1) * g).astype(np.float16)
        r64 = r.astype(np.float64)
        r = np.sqrt(b2 * r64 * r64 + (1 - b2) * g * g).astype(np.float16)
        ms, rs = 1 / (1 - b1**t), 1 / math.sqrt(1 - b2**t)
        w = w - LR * m.astype(np.float64) * ms / (r.astype(np.float64) * rs + eps * S)
    return w


def test_adam16_compensation_tracks_the_exact_path(dev):
    """Updates below half an f16 ulp are kept with compensation and lost
    without it: the compensated weight minus its error buffer (what the
    last write added in excess) tracks the exact path to a fraction of the
    plain weights' distance, and the weight itself stays within 3 ulp."""
    w0 = RNG.standard_normal((32, 32)).astype(np.float16)
    grads = [(RNG.standard_normal((32, 32)) * 1e-3 * S).astype(np.float16)]
    grads *= STEPS
    exact = exact_path(w0, grads)
    ulp = np.spacing(np.abs(exact.astype(np.float16))).astype(np.float64)

    kahan, err = adam_steps(w0, grads, compensate=True)
    plain, _ = adam_steps(w0, grads, compensate=False)

    assert (np.abs(kahan - exact) / ulp).max() <= 3
    kept = np.abs(kahan - err - exact).mean()
    assert kept < 0.25 * np.abs(plain - exact).mean()


def cross_entropy(xs, ys, weight, bias):
    """CPU f32: a Linear module's mean cross entropy over a batch, and its
    gradients of weight and bias."""
    logits = xs.astype(np.float32) @ weight.T + bias
    p = np.exp(logits - logits.max(1, keepdims=True))
    p /= p.sum(1, keepdims=True)
    B = len(ys)
    miss = p - np.eye(weight.shape[0], dtype=np.float32)[ys]
    loss = -np.log(p[np.arange(B), ys]).mean()
    return loss, miss.T @ xs.astype(np.float32) / B, miss.mean(0)


def adam_first_step(w, grad, lr):
    """Adam's first update of `w`, its moments' bias correction undone: a step
    of `lr` against each gradient's sign."""
    return w - lr * np.sign(grad)


def test_a_module_trains_one_step(dev):
    """Forward, backward and Adam16 of an `nn.Linear` module on the card:
    every weight moves, the loss on the same batch falls, and the loss is CPU
    f32's on that batch."""
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = nn.Linear(64, 32)
    weight, bias = model.weight.numpy(), model.bias.numpy()
    place(model)
    assert all(t.device == "KTPU" for t in get_state_dict(model).values())

    xs = RNG.standard_normal((32, 64)).astype(np.float16)
    ys = RNG.integers(0, 32, 32).astype(np.int32)
    x, y = Tensor(xs, device="KTPU"), Tensor(ys, device="KTPU")
    before = [t.numpy() for t in get_parameters(model)]
    opt = Adam16(get_parameters(model), lr=1e-2)

    with Context(TRAINING=1):
        loss = model(x).sparse_categorical_crossentropy(y)
        opt.zero_grad()
        opt.scale(loss).backward()
        # The loss realised with the step: read before the weights are written.
        Tensor.realize(loss, *opt.schedule_step())
    first = loss.item()
    again = model(x).sparse_categorical_crossentropy(y).item()

    after = [t.numpy() for t in get_parameters(model)]
    assert all(not np.array_equal(a, b) for a, b in zip(before, after))
    assert again < first
    assert first == pytest.approx(cross_entropy(xs, ys, weight, bias)[0], rel=0.02)


@pytest.mark.parametrize("B", [1, 8, 32])
def test_a_step_reads_every_label(dev, B):
    """A step against CPU arithmetic, the loss and the weights it writes: the
    one-hot of its labels, which runs fused with its neighbour, holds each
    row's own label."""
    LR = 1e-2
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = nn.Linear(64, 32)
    weight, bias = model.weight.numpy(), model.bias.numpy()
    place(model)
    xs = RNG.standard_normal((B, 64)).astype(np.float16)
    ys = RNG.integers(0, 32, B).astype(np.int32)
    want, grad_w, grad_b = cross_entropy(xs, ys, weight, bias)

    opt = Adam16(get_parameters(model), lr=LR)
    with Context(TRAINING=1):
        loss = model(Tensor(xs, device="KTPU")).sparse_categorical_crossentropy(
            Tensor(ys, device="KTPU")
        )
        opt.zero_grad()
        opt.scale(loss).backward()
        Tensor.realize(loss, *opt.schedule_step())
    for got, start, grad in (
        (model.weight, weight, grad_w),
        (model.bias, bias, grad_b),
    ):
        # Within the MXFP7 product's 0.03 of the largest, a sign is not held.
        kept = np.abs(grad) > 0.05 * np.abs(grad).max(-1, keepdims=True)
        step = got.numpy()[kept] - adam_first_step(start, grad, LR)[kept]
        assert np.abs(step).max() < 0.25 * LR
    assert loss.item() == pytest.approx(want, rel=0.02)


def test_a_batch_of_one_updates_every_row_of_a_layer(dev):
    """At batch 1 the gradient of a [128, 400] layer is a statistic times a
    band, and Adam16 folds it into the rms kernel that reads the weights:
    every parameter's first step against CPU f32 gradients of the same step."""
    LR = 1e-2
    rng = np.random.default_rng(3)
    xs = rng.standard_normal((1, 400)).astype(np.float16)
    ys = np.eye(10, dtype=np.float16)[rng.integers(0, 10, 1)]
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        cpu = MLP(128, 400)
        start = [p.numpy().copy() for p in get_parameters(cpu)]
        with Context(TRAINING=1):
            for p in get_parameters(cpu):
                p.requires_grad = True
            logits = cpu(Tensor(xs.astype(np.float32)))
            want = -(logits.log_softmax() * Tensor(ys.astype(np.float32))).sum(-1)
            want.mean().backward()
        grads = [p.grad.numpy() for p in get_parameters(cpu)]
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = MLP(128, 400)
    place(model)
    opt = Adam16(get_parameters(model), lr=LR)
    with Context(TRAINING=1):
        loss, _ = train_step(
            model, opt, Tensor(xs, device="KTPU"), Tensor(ys, device="KTPU")
        )
    assert loss.item() == pytest.approx(want.mean().item(), rel=0.02)
    for p, w, g in zip(get_parameters(model), start, grads):
        kept = np.abs(g) > 0.05 * np.abs(g).max(-1, keepdims=True)
        step = p.numpy()[kept] - adam_first_step(w, g, LR)[kept]
        assert np.abs(step).max() < 0.25 * LR
