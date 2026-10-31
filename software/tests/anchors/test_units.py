"""The mover and the clusters against the Verilated card: a tiny run of each unit's
words on the card model and on the device double, which must agree. `KTPU_ANCHOR_CARD`
is a card model's build directory, its board `KTPU_ANCHOR_BOARD`."""

import os

import numpy as np
import pytest
from tinygrad import Tensor

os.environ.setdefault("KTPU_COMPOSED", "on")

from kohakutpu.application import ktpugrad  # noqa: F401
from kohakutpu.application.runtime import device as card

MODEL = os.environ.get("KTPU_ANCHOR_CARD")
BOARD = os.environ.get("KTPU_ANCHOR_BOARD", "vlt_v9t6s_1n")
#: Steps of f16 two float paths may round apart.
ULPS = 8

R = np.random.default_rng(0)
A = R.normal(size=(16, 32))
B = R.normal(size=(32, 32))

KERNELS = {
    "copy": lambda a, b: a.reshape(32, 16) + 0.0,
    "transpose": lambda a, b: a.transpose() + 0.0,
    "contraction": lambda a, b: a @ b,
    "drain_add": lambda a, b: (a @ b) + (a @ b),
    "pad": lambda a, b: a.pad(((1, 1), (0, 0))) + 0.0,
}


@pytest.fixture(scope="module")
def devices():
    if not MODEL:
        pytest.skip("no card model")
    verilated = card.Device(kind="sim", build=MODEL, board=BOARD, nodes=1)
    double = card.Device(kind="double", board=BOARD, nodes=1)
    yield verilated, double
    verilated.close()
    double.close()


def run(dev, f):
    with card.attached(dev):
        args = [Tensor(x.astype(np.float16), device="KTPU") for x in (A, B)]
        return f(*args).numpy()


@pytest.mark.parametrize("name", KERNELS)
def test_units_end_as_the_double_says(devices, name):
    verilated, double = devices
    got = run(verilated, KERNELS[name])
    want = run(double, KERNELS[name])
    apart = np.abs(got.astype(np.float64) - want.astype(np.float64))
    assert apart.max() <= ULPS * np.spacing(np.abs(want)).max()
