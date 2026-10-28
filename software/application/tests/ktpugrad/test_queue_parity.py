"""The synchronous card result witnesses the same deterministic queued step."""

import numpy as np
from tinygrad import Tensor
from tinygrad.helpers import Context

from kohakutpu.application.ktpugrad.nn import Adam16


def test_queued_step_matches_synchronous_card(dev):
    outputs = []
    original = dev.queued
    try:
        for mode in (False, True):
            dev.drain()
            dev.queued = mode
            x = Tensor(
                np.linspace(-1, 1, 32 * 64, dtype=np.float16).reshape(32, 64),
                device="KTPU",
            )
            w = Tensor(
                np.linspace(-0.1, 0.1, 32 * 64, dtype=np.float16).reshape(32, 64),
                device="KTPU",
            )
            opt = Adam16([w], lr=1e-3)
            with Context(TRAINING=1):
                loss = (x @ w.T).square().mean()
                opt.zero_grad()
                opt.scale(loss).backward()
                Tensor.realize(loss, *opt.schedule_step())
            outputs.append((loss.numpy(), w.numpy()))
        for synchronous, queued in zip(outputs[0], outputs[1], strict=True):
            np.testing.assert_array_equal(queued, synchronous)
    finally:
        dev.drain()
        dev.queued = original
