"""Bounded tier-1 library smoke: one 128-element activation on v9t6s."""

import json
import os
import pathlib
import runpy
import time

import numpy as np
import pytest
from tinygrad.helpers import Context
from tinygrad.tensor import Tensor

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms, generated
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs

# The batch-one LeNet forward output of the `library` mode on v9t6s.
LIBRARY_VALUES = (
    pathlib.Path(__file__).parent / "fixtures/lenet_forward_library_values.json"
)


def test_library_activation_on_model(dev, tmp_path):
    kernel, given = V.flat_kernel(X.op("add", X.Load(0), 1.0), ("f16",), 128, "f16", 2)
    traced = kernel.trace(given, machine=K.dsl_machine(dev.machine))
    source = json.dumps(traced.graph)
    rs._rs.library_prebuild(str(tmp_path), source, rs.machine_json(dev.machine))
    previous = dev.library, dev.library_only
    x, y = dev.alloc(256), dev.alloc(256)
    data = np.arange(128, dtype=np.float16) / 128
    started = time.perf_counter()
    try:
        dev.write(x, data.tobytes())
        dev.library, dev.library_only = str(tmp_path), True
        output = dev.compile_graph(source)
        dev.run(
            generated.from_output(dev, traced.graph, output, traced),
            {"in0": x, "out": y},
        )
        actual = np.frombuffer(dev.read(y, 256), np.float16)
        np.testing.assert_array_equal(actual, data + np.float16(1))
        assert dev.counters["compile_calls"] == 0
        print(
            "library model activation seconds",
            time.perf_counter() - started,
            dev.counters,
        )
    finally:
        dev.library, dev.library_only = previous
        dev.free(x)
        dev.free(y)


@pytest.mark.parametrize("keep", [False, True])
def test_class_lanes_values(dev, keep):
    src = np.arange(64, dtype=np.float16)
    initial = np.arange(32, dtype=np.float16) + 200
    made = generated.relayout_plan(
        "lanes.plan", 64, ((3, 17), (7, 1)), 0, ((3, 9), (7, 1)), 0, 32, keep
    )
    plan = generated.lanes_tables(dev, made)
    x, y = dev.alloc(128), dev.alloc(64)
    try:
        dev.write(x, src.tobytes())
        dev.write(y, (initial if keep else np.zeros(32, np.float16)).tobytes())
        for c in plan["classes"]:
            generated.launch(
                dev,
                "lanes.class_kernel",
                {
                    "class": c["class"],
                    "src_words": plan["src_words"],
                    "dst_words": plan["dst_words"],
                    "core_policy": "all",
                },
                {
                    "src": x,
                    "dst": y,
                    "index": forms.table(dev, c["index"], np.float32),
                    "mask": forms.table(dev, c["masks"]),
                },
            )
        actual = np.frombuffer(dev.read(y, 64), np.float16)
        expected = initial.copy() if keep else np.zeros(32, np.float16)
        for row in range(3):
            expected[row * 9 : row * 9 + 7] = src[row * 17 : row * 17 + 7]
        np.testing.assert_array_equal(actual, expected)
    finally:
        dev.free(x)
        dev.free(y)


@pytest.mark.parametrize("mode", ["library", "direct"])
def test_lenet_library_forward_batch_one(dev, mode, tmp_path):
    root = pathlib.Path(D.ROOT)
    # The values and the CPU comparison go to KTPU_LIBRARY_MODEL_OUT when set.
    output = pathlib.Path(os.environ.get("KTPU_LIBRARY_MODEL_OUT") or tmp_path)
    output.mkdir(parents=True, exist_ok=True)
    harness = runpy.run_path(str(root / "scripts/py/templates/prebuild.py"))
    previous = dev.library, dev.library_only
    dev.library = str(root / "build/templates") if mode == "library" else None
    dev.library_only = mode == "library"
    started = time.perf_counter()
    try:
        actual = harness["lenet"](1, True).numpy()
        dev.drain()
        if mode == "library":
            assert dev.counters["compile_calls"] == 0
        else:
            assert dev.counters["compile_calls"] > 0
            saved = json.loads(LIBRARY_VALUES.read_text(encoding="utf-8"))
            np.testing.assert_array_equal(actual, np.array(saved["output"], np.float16))
        assert dev.counters["library_misses"] == 0
        assert np.isfinite(actual).all()
        result = {
            "batch": 1,
            "seconds": time.perf_counter() - started,
            "output": actual.tolist(),
            "counters": dev.counters,
        }
        (output / f"{mode}-values.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        print(result)
        if mode == "direct":
            example = runpy.run_path(
                str(root / "software/application/examples/tinygrad/mnist_mlp.py")
            )
            Tensor.manual_seed(0)
            with Context(DEV="CPU"):
                reference = example["LeNet"]()
                expected = reference(
                    Tensor(np.zeros((1, 784), np.float32), device="CPU")
                ).numpy()
            error = float(
                np.max(np.abs(actual.astype(np.float32) - expected))
                / np.max(np.abs(expected))
            )
            result.update(cpu_output=expected.tolist(), relative_max_error=error)
            (output / "comparison.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
            print("CPU relative maximum error", error)
    finally:
        dev.library, dev.library_only = previous
