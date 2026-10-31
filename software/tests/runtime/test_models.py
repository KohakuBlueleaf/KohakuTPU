"""Tiny models against tinygrad's CPU backend, nothing attached."""

import numpy as np
from examples.tinygrad.mnist_models import LeNet
from examples.tinygrad.vit_tiny import seeded_model
from tinygrad import Tensor
from tinygrad.helpers import Context
from tinygrad.nn.state import get_state_dict

from kohakutpu.application import ktpugrad
from kohakutpu.application.ktpugrad.nn import place

#: Largest error, of the largest logit: f16 through MXFP7 contractions.
LOGITS = 0.04


def close(got, want):
    worst = np.abs(got.astype(np.float64) - want).max()
    assert worst <= LOGITS * np.abs(want).max(), f"{worst} of {np.abs(want).max()}"
    assert (got.argmax(-1) == want.argmax(-1)).mean() >= 0.75


def lenet():
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        return LeNet()


def test_lenet_forward(dev):
    x = np.random.default_rng(0).random((8, 784))
    with Context(TRAINING=0):
        ktpugrad.uninstall()
        reference = lenet()
        want = reference(Tensor(x.astype(np.float32), device="CPU")).numpy()
        weights = {k: t.numpy() for k, t in get_state_dict(reference).items()}
        ktpugrad.install()
        model = lenet()
        for k, t in get_state_dict(model).items():
            t.replace(Tensor(weights[k], device="CPU"))
        place(model)
        got = model(Tensor(x.astype(np.float16), device="KTPU")).numpy()
    close(got, want)


def test_vit_forward(dev):
    x = np.random.default_rng(1).standard_normal((1, 3, 32, 32))
    ktpugrad.uninstall()
    want = seeded_model(1, 17, 64, 32)(
        Tensor(x.astype(np.float32), device="CPU")
    ).numpy()
    model = seeded_model(1, 17, 64, 32)
    ktpugrad.install()
    place(model)
    got = model(Tensor(x.astype(np.float16), device="KTPU")).numpy()
    close(got, want)
