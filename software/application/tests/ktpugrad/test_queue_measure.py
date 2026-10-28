"""Opt-in one-workload measurement, serialized by the pytest model lock."""

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

from kohakutpu.application.runtime.device import Device

ROOT = Path(__file__).resolve().parents[4]
# The examples are a package rooted at software/application.
sys.path.insert(0, str(ROOT / "software/application"))
measure = importlib.import_module("examples.tinygrad.queue_measure").measure


def test_queue_measure(tmp_path):
    workload = os.environ.get("KTPU_QUEUE_MEASURE")
    if not workload:
        pytest.skip("set KTPU_QUEUE_MEASURE to one workload")
    # Appended to KTPU_QUEUE_MEASURE_OUT when set.
    output = Path(os.environ.get("KTPU_QUEUE_MEASURE_OUT") or tmp_path / "measure.json")
    dev = Device(kind="sim")
    dev.card.alarm = 90
    result = None
    try:
        result = measure(dev, workload)
    finally:
        if result is not None and "error" in result:
            # Terminating this model proves no pending DMA can use its memory.
            dev.card.t.close()
            dev.closed = True
            for arena in dev.memory.arenas():
                arena.close()
        else:
            dev.close()
    data = (
        json.loads(output.read_text())
        if output.exists()
        else {"board": "vlt_v9t6s_1n", "measurements": []}
    )
    data["measurements"].append(result)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    assert "error" not in result, result.get("error")
    assert result["events"] and all("phases" in e for e in result["events"])
