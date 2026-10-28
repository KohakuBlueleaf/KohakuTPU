"""Offline-library model values and compilation refusal on the production device."""

import json
import runpy
import time
from pathlib import Path

import numpy as np
from harness.registry import mutant, patched, runner
from tinygrad.helpers import Context
from tinygrad.tensor import Tensor

from kohakutpu.application.runtime import device as D

ROOT = Path(D.ROOT)
ORIGINAL_READ = D.Device.read


@mutant("library/output-offset")
def output_offset():
    def read(dev, address, size):
        raw = ORIGINAL_READ(dev, address, size)
        # A ten-logit output is returned with the wrong decoded values.
        if size == 20:
            return (np.frombuffer(raw, np.float16) + np.float16(1)).tobytes()
        return raw

    with patched(D.Device, "read", read):
        yield


@runner("library.lenet.card", shaped=True)
def lenet(inp, ctx):
    dev = ctx["session"].dev
    D.attach(dev)
    harness = runpy.run_path(str(ROOT / "scripts/py/templates/prebuild.py"))
    example = runpy.run_path(
        str(ROOT / "software/application/examples/tinygrad/mnist_mlp.py")
    )
    previous = dev.library, dev.library_only
    before = dict(dev.counters)
    started = time.perf_counter()
    try:
        dev.library, dev.library_only = str(ROOT / "build/templates"), True
        actual = harness["lenet"](1, True).numpy().astype(np.float32)
        dev.drain()
        Tensor.manual_seed(inp["seed"])
        with Context(DEV="CPU"):
            model = example["LeNet"]()
            expected = model(
                Tensor(np.zeros((1, 784), np.float32), device="CPU")
            ).numpy()
        error = float(np.max(np.abs(actual - expected)) / np.max(np.abs(expected)))
        counters = {key: value - before[key] for key, value in dev.counters.items()}
        elapsed = time.perf_counter() - started
        evidence = {
            "batch": 1,
            "seed": inp["seed"],
            "relative_max_error": error,
            "seconds": elapsed,
            "counters": counters,
        }
        print(json.dumps(evidence), flush=True)
        assert elapsed < 600, evidence
        return {
            "within": bool(np.isfinite(actual).all() and error <= inp["tolerance"]),
            "zero_compiles": counters["compile_calls"] == 0,
            "library_used": counters["library_hits"] > 0
            and counters["library_misses"] == 0,
        }
    finally:
        dev.library, dev.library_only = previous
