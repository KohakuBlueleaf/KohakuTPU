"""Offline integrity and refusal witnesses for measured timing provenance."""

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/py"))

from machine import table
from measure import vc_timing

replay = importlib.import_module("kohakutpu.simulation.verilator.replay")


def test_records_rederive_from_raw_runs():
    for path in (ROOT / "machines/measured").glob("vc_timing.*.json"):
        record = json.loads(path.read_text())
        assert vc_timing.values(record["probes"]) == record["parameters"]
        for probe in record["probes"]:
            assert probe["fit"] == vc_timing.fit(
                [r["K"] for r in probe["runs"]],
                [r["elapsed"] for r in probe["runs"]],
            )
            assert all(r["faults"] == 0 and not r["warn"] for r in probe["runs"])


def test_missing_record_refused():
    image = SimpleNamespace(
        ver="not-measured",
        defines={"VEC_CORE_V2": "1", "V2_RW": "8"},
        config=lambda _: ("v21", "test"),
    )
    with pytest.raises(ValueError, match="no unique measured VC timing"):
        table.vc_timing("math_math")(image)


def test_record_value_mutation_is_detected():
    record = json.loads((ROOT / "machines/measured/vc_timing.v21.mq1.json").read_text())
    record["parameters"]["math_math"]["value"] += 1
    assert vc_timing.values(record["probes"]) != record["parameters"]


def test_failed_build_does_not_use_existing_executable(monkeypatch, tmp_path):
    executable = tmp_path / "vsim"
    executable.touch()
    monkeypatch.setattr(replay, "vsim", lambda _: executable)
    monkeypatch.setattr(
        replay.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="failed", stderr=""),
    )
    with pytest.raises(RuntimeError, match="did not build"):
        replay.build_model(ROOT / "build/r63-build-refusal")


def test_replay_paths_fit_the_bench_filename_register(monkeypatch, tmp_path):
    build = tmp_path / ("long-directory-" * 8)
    executable = replay.vsim(build)
    executable.parent.mkdir(parents=True)
    executable.touch()
    seen = []

    def run(args, **kwargs):
        seen.extend(args[1:4])
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(replay.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="no replay result"):
        replay.replay([], b"", build=build)
    assert len(seen) == 3
    assert all(len(arg.split("=", 1)[1].encode()) < 128 for arg in seen)
    assert all(not Path(arg.split("=", 1)[1]).is_absolute() for arg in seen)
