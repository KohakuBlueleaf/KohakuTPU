"""Tinygrad's lowering against tinygrad's CPU backend: small tensors through the
planner and forms, each kernel on the device double."""

import os

import numpy as np
import pytest
from tinygrad.tensor import Tensor

# Composed reads are part of the lowering; their kernels here compile in a blink.
os.environ.setdefault("KTPU_COMPOSED", "on")

from kohakutpu.application import ktpugrad  # noqa: F401
from kohakutpu.application.runtime import device as card

#: The stages' machine, so a harvest fits their corpus.
BOARD = "multimesh_v9t8"
#: How far f16 on the device may sit from f32 on the CPU, relative to the largest magnitude.
TOLERANCE = 2e-3


@pytest.fixture(scope="session")
def dev():
    d = card.attach(card.Device(kind="double", board=BOARD, nodes=1))
    yield d
    d.close()


@pytest.fixture
def same(dev):
    """`f` of the host arrays `xs` on the device, f16, against the CPU, f32."""

    def check(f, *xs, tolerance=TOLERANCE):
        want = f(*[Tensor(x.astype(np.float32), device="CPU") for x in xs]).numpy()
        got = f(*[Tensor(x.astype(np.float16), device="KTPU") for x in xs]).numpy()
        assert got.shape == want.shape
        scale = max(float(np.abs(want).max()), 1e-3)
        worst = float(np.abs(got.astype(np.float64) - want).max())
        assert worst <= tolerance * scale, f"{worst} of {scale}"

    return check
