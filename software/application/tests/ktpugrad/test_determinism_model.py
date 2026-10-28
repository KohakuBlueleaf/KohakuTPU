"""Opt-in small Verilated executable check; invoke separately with -n 1."""

import json
import os
from pathlib import Path

import numpy as np
import pytest
from tinygrad import Tensor
from tinygrad.engine.jit import TinyJit

from kohakutpu.application.checks.determinism import DeterminismChecker, RepeatComplete
from kohakutpu.application.runtime import device as D


def test_repetition_on_readonly_prebuilt_model(tmp_path):
    build = os.environ.get("KTPU_DETERMINISM_MODEL")
    if not build:
        pytest.skip("set KTPU_DETERMINISM_MODEL to the read-only prebuilt model")
    # The model log and reports go to KTPU_DETERMINISM_OUT when set.
    output = Path(os.environ.get("KTPU_DETERMINISM_OUT") or tmp_path)
    output.mkdir(parents=True, exist_ok=True)
    x = np.arange(32 * 32, dtype=np.float16).reshape(32, 32) / 1024

    def step(value):
        return ((value + 1).realize().sum(0).realize() * 0.25).realize()

    # Compile only offline, then reuse the exact in-memory compiled objects.
    offline = D.Device("offline", board="vlt_v9t6s_1n")
    with D.attached(offline), DeterminismChecker(offline, 2):
        step(Tensor(x, device="KTPU"))
        a = Tensor(x, device="KTPU").realize()
        (a @ a).realize()
    kernels = dict(offline.kernels)
    offline.close()
    dev = D.Device(
        "sim", board="vlt_v9t6s_1n", build=build, nodes=1, log=output / "model.log"
    )
    dev.kernels.update(kernels)
    reports = []
    try:
        with D.attached(dev), DeterminismChecker(dev, 3) as checker:
            checker.active = True
            step(Tensor(x, device="KTPU"))
        reports.append(checker.report())
        assert reports[-1]["passed"]
        with D.attached(dev), DeterminismChecker(dev, 2) as checker:
            fn = TinyJit(step)
            for i in range(3):
                checker.active = i == 2
                fn(Tensor(x, device="KTPU"))
        reports.append(checker.report())
        assert reports[-1]["passed"] and checker.graph_replays > 0
        prefix = reports[0]["packages"][0]["name"]
        with (
            D.attached(dev),
            DeterminismChecker(dev, 5, package_prefix=prefix) as checker,
        ):
            checker.active = True
            with pytest.raises(RepeatComplete):
                step(Tensor(x, device="KTPU"))
        reports.append(checker.report())
        assert reports[-1]["passed"] and reports[-1]["packages"][0]["completed"] == 5
        with D.attached(dev), DeterminismChecker(dev, 3) as checker:
            checker.active = True
            a = Tensor(x, device="KTPU").realize()
            (a @ a).realize()
        reports.append(checker.report())
        assert reports[-1]["passed"]
        assert any(p["kind"] == "quant" for p in reports[-1]["packages"])
        assert dev.counters["compile_calls"] == 0
    finally:
        (output / "model.json").write_text(
            json.dumps(dict(reports=reports, counters=dev.counters), indent=2),
            encoding="utf-8",
        )
        dev.close()
