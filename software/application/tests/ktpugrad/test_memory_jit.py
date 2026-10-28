"""TinyJit replays current inputs on the model with main-heap pins."""

import numpy as np
from tinygrad import Tensor, TinyJit
from tinygrad.dtype import dtypes

from kohakutpu.application.ktpugrad.device import KTPUGraph


def test_capture_replay_numerics_and_lifetime(dev, monkeypatch):
    graphs = []
    original = KTPUGraph.__call__

    def observe(self, *args, **kwargs):
        if self not in graphs:
            graphs.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(KTPUGraph, "__call__", observe)

    @TinyJit
    def step(x):
        doubled = (x * 2).contiguous().realize()
        return (doubled + 1).contiguous().realize()

    for i in range(9):
        raw = (np.arange(144, dtype=np.float16) / 16 + i).astype(np.float16)
        x = Tensor(raw, device="KTPU", dtype=dtypes.half).realize()
        y = step(x)
        np.testing.assert_array_equal(y.numpy(), raw * 2 + 1)
    assert graphs and sum(g.counts["replayed"] for g in graphs) > 0
    assert not dev.private
    logs = [g.log[1] for g in graphs if g.log is not None]
    assert logs and all(log.references for log in logs)
    for log in logs:
        refs = [(arena, at, arena.references[at]) for arena, at in log.references]
        log.close()
        assert all(
            arena.references.get(at, 0) == count - 1 for arena, at, count in refs
        )
        assert not log.references and log.closed
