"""Training on the KTPU device: `place` and `Adam16` against an independent
witness, and one backward step of a module."""

import math

import numpy as np
import pytest
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


def test_a_module_trains_one_step(dev):
    """Forward, backward and Adam16 of an `nn.Linear` module on the card:
    every weight moves and the loss on the same batch falls."""
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = nn.Linear(64, 32)
    place(model)
    assert all(t.device == "KTPU" for t in get_state_dict(model).values())

    x = Tensor(RNG.standard_normal((32, 64)).astype(np.float16), device="KTPU")
    y = Tensor(RNG.integers(0, 32, 32).astype(np.int32), device="KTPU")
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


@pytest.mark.parametrize("B", [8, 32])
def test_a_step_reads_every_label(dev, B):
    """The loss of a step against CPU arithmetic: the one-hot of its labels,
    which runs fused with its neighbour, holds each row's own label."""
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = nn.Linear(64, 32)
    weight, bias = model.weight.numpy(), model.bias.numpy()
    place(model)
    xs = RNG.standard_normal((B, 64)).astype(np.float16)
    ys = RNG.integers(0, 32, B).astype(np.int32)
    logits = xs.astype(np.float32) @ weight.T + bias
    log_p = logits - np.log(np.exp(logits).sum(1, keepdims=True))
    want = -log_p[np.arange(B), ys].mean()

    opt = Adam16(get_parameters(model), lr=1e-2)
    with Context(TRAINING=1):
        loss = model(Tensor(xs, device="KTPU")).sparse_categorical_crossentropy(
            Tensor(ys, device="KTPU")
        )
        opt.zero_grad()
        opt.scale(loss).backward()
        Tensor.realize(loss, *opt.schedule_step())
    assert loss.item() == pytest.approx(want, rel=0.02)
