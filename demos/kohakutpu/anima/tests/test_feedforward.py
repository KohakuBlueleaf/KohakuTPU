"""Unit tests for FeedForward / MLP module on KohakuTPU."""

import ktpugrad
import numpy as np
import pytest
from tinygrad import Tensor

from demos.kohakutpu.anima.models.feedforward import FeedForward


@pytest.fixture(scope="module", autouse=True)
def setup_ktpu():
    ktpugrad.install()
    yield
    ktpugrad.uninstall()


def test_feedforward_on_ktpu():
    d_model = 64
    d_ff = 128
    ff = FeedForward(d_model=d_model, d_ff=d_ff, device="KTPU")

    x = Tensor(np.random.randn(64, d_model).astype(np.float16), device="KTPU")
    out = ff(x).realize()
    assert out.shape == (64, d_model)
    assert not np.isnan(out.numpy()).any()


def test_feedforward_3d_input():
    d_model = 64
    d_ff = 128
    ff = FeedForward(d_model=d_model, d_ff=d_ff, device="KTPU")

    x = Tensor(np.random.randn(1, 64, d_model).astype(np.float16), device="KTPU")
    out = ff(x).realize()
    assert out.shape == (1, 64, d_model)
    assert not np.isnan(out.numpy()).any()
