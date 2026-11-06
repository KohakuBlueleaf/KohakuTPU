"""Eager contractions share the device grant with the native split planner."""

import numpy as np
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.runtime import device as card


def test_eager_matmul_split_preserves_the_single_node_result(monkeypatch):
    ktpugrad.install()
    rng = np.random.default_rng(47)
    a = (rng.standard_normal((64, 256)) / 8).astype(np.float16)
    b = (rng.standard_normal((256, 64)) / 8).astype(np.float16)
    outputs = []
    for nodes in (1, 4):
        if nodes == 1:
            monkeypatch.delenv("KTPU_SPLIT_OPTION", raising=False)
        else:
            monkeypatch.setenv("KTPU_SPLIT_OPTION", "m")
        dev = card.Device(kind="double", board="multimesh_v9t8", nodes=nodes)
        try:
            with card.attached(dev):
                x = Tensor(a, device="KTPU").realize()
                w = Tensor(b, device="KTPU").realize()
                outputs.append((x @ w).numpy().tobytes())
                (run,) = [r for r in dev.runs if r.kind == "matmul"]
                assert run.info["active_nodes"] == nodes
                if nodes == 4:
                    assert run.info["option_id"] == "m"
        finally:
            dev.close()
    assert outputs[1] == outputs[0]
