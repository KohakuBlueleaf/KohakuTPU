"""Bounded multi-node POC witnesses; the model measurement runs on the card
tests' device (`conftest.card_device`) over each of its nodes."""

import pathlib
import runpy

import numpy as np
import pytest

from kohakutpu.application.runtime.device import Device

POC = runpy.run_path(
    str(pathlib.Path(__file__).resolve().parents[2] / "examples/multinode/poc.py")
)


def test_multinode_recipes_compile():
    dev = Device(kind="offline", board=POC["BOARD"], nodes=2)
    try:
        for builder, expected in (
            (POC["matmul_kernel"], 2),
            (POC["local_gradient_kernel"], 2),
            (POC["reduce_update_kernel"], 2),
        ):
            kernel, given = builder()
            compiled = dev.specialise(kernel, given)
            assert not compiled.verdict["refused"], compiled.verdict
            assert set(compiled.packages) == set(range(expected))
        assert {a["node"] for a in compiled.scratch or ()} == set()
        grad, given = POC["local_gradient_kernel"]()
        staged = dev.specialise(grad, given)
        assert {a["node"] for a in staged.scratch} == {0, 1}
        assert all(a["space"] == "stage" for a in staged.scratch)
        one, given = POC["matmul_kernel"](nodes=1)
        assert set(dev.specialise(one, given).packages) == {0}
    finally:
        dev.close()


def test_training_witness_rejects_unreduced_or_unscaled_gradients():
    arrays = POC["training_data"]()
    gradients, mean, weights = POC["oracle"](arrays)
    actual = {
        f"{key}{node}": value
        for node in range(2)
        for key, value in (("g", gradients[node]), ("reduced", mean), ("next", weights))
    }
    POC["witness"](arrays, actual)
    broken = dict(actual)
    broken["next1"] = (
        POC["e8m15"]
        .value(arrays["w1"].astype(np.float64) - 0.125 * gradients[1] / 32)
        .astype(np.float16)
    )
    with pytest.raises(AssertionError):
        POC["witness"](arrays, broken)
    broken = dict(actual)
    broken["reduced0"] = mean * 2
    with pytest.raises(AssertionError):
        POC["witness"](arrays, broken)


def test_reduction_oracle_rounds_each_lane_add():
    arrays = POC["training_data"]()
    gradients, mean, _ = POC["oracle"](arrays)
    # Captured nonzero-weight model output, where bypassing E8M15 changes FP16.
    assert mean[12, 3] == np.float16(0.0021514892578125)
    unrounded = ((gradients[0] + gradients[1]) / 64).astype(np.float16)
    assert unrounded[12, 3] != mean[12, 3]


def test_two_node_model_measurement(dev):
    result = POC["measure"](dev)
    assert result["training_exact"] and result["matmul_exact"]


def test_model_measurement_rejects_a_negated_operand(dev, monkeypatch):
    class Negated(POC["Experiment"]):
        def put(self, name, array):
            super().put(name, -array if name == "b" else array)

    monkeypatch.setitem(POC["measure"].__globals__, "Experiment", Negated)
    with pytest.raises(AssertionError, match="not equal"):
        POC["measure"](dev, case="matmul")
