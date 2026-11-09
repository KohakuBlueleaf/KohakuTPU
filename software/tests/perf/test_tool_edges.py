"""Static-data witnesses for tool boundaries, without building or launching a card."""

import gzip
import importlib.util
import json
import pathlib
import sys
from types import SimpleNamespace as NS
from typing import Any, cast

import pytest
import record

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.device.common import Submitted

TOOLS = pathlib.Path(__file__).resolve().parents[3] / "measurements/tools"


def tool(name, monkeypatch):
    monkeypatch.syspath_prepend(str(TOOLS))
    spec = importlib.util.spec_from_file_location(
        f"round_witness_{name}", TOOLS / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_readback_must_be_locked_and_match_recorder_profile(monkeypatch):
    driver = tool("card_round", monkeypatch)
    board = {
        "vco_mhz": 1200,
        "profiles": {"mid": {"sys": 200, "noc": 200, "vec": 200, "mat2x": 400}},
    }
    clocks: dict[str, dict[str, Any]] = {
        str(n): dict(locked=True, **board["profiles"]["mid"]) for n in range(4)
    }
    driver.validate_clocks(clocks, board, "mid")
    clocks["3"]["sys"] = 100
    with pytest.raises(ValueError, match="differs"):
        driver.validate_clocks(clocks, board, "mid")
    clocks["3"]["sys"] = 200
    clocks["3"]["locked"] = False
    with pytest.raises(ValueError, match="locked"):
        driver.validate_clocks(clocks, board, "mid")


def test_concurrent_driver_lock_stops_without_calling_card(tmp_path, monkeypatch):
    driver = tool("card_round", monkeypatch)
    (tmp_path / ".driver-lock").mkdir()
    calls = []
    assert driver.run_matrix([], tmp_path, "r1", lambda *args: calls.append(args)) == 2
    assert not calls
    assert (tmp_path / "STOP.json").exists()


def test_retained_regions_with_the_same_runtime_name_keep_distinct_code(tmp_path):
    trace = record.Trace(tmp_path)
    a = NS(
        name="captured_split", l1_text="fn captured_split.l1()\n  done\n", l2_text=""
    )
    b = NS(
        name="captured_split",
        l1_text="fn captured_split.l1()\n  wait\n  done\n",
        l2_text="",
    )
    first, second = trace.kernel(a, unique=True), trace.kernel(b, unique=True)
    assert first != second
    assert trace.kernel(a, unique=True) == first
    assert f"fn {first}.l1(" in (tmp_path / "kernels" / f"{first}.l1").read_text()
    assert f"fn {second}.l1(" in (tmp_path / "kernels" / f"{second}.l1").read_text()
    assert a.name == b.name == "captured_split"
    assert "fn captured_split.l1(" in a.l1_text  # runtime object was not renamed
    trace.save({})


def test_real_observer_uses_grouped_retirement_not_node_result_order(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PERF_CLOCKS", raising=False)

    def init(self, *args, **kwargs):
        self.card = NS(nodes=(0, 1))
        self.runs, self.counters = [], {}

    def collect(self, submitted):
        if submitted.run is None:
            submitted.run = NS(
                name=submitted.name,
                kind=submitted.kind,
                wall=0.1,
                cycles=submitted.result["cycles"],
                info=submitted.info,
            )
            self.runs.append(submitted.run)
        return submitted.run

    monkeypatch.setattr(D.Device, "__init__", init)
    monkeypatch.setattr(D.Device, "_collect_one", collect)
    # Observer installation also wraps these methods; restore on fixture exit.
    monkeypatch.setattr(D.Device, "close", D.Device.close)
    monkeypatch.setattr(D.Device, "read", D.Device.read)
    finish = record.on_card(tmp_path, measure=False, traced=False)
    dev = D.Device("card")
    for seq, kind, cycles in ((0, "quant", (10, 20)), (1, "matmul", (40, 30))):
        submission = NS(
            name=f"k{seq}",
            kind=kind,
            info={"seq": seq},
            run=None,
            result={
                "cycles": max(cycles),
                "nodes": {n: {"cycles": c} for n, c in enumerate(cycles)},
            },
        )
        dev._collect_one(cast(Submitted, submission))
        dev._collect_one(cast(Submitted, submission))
    finish()
    rows = [
        json.loads(line) for line in (tmp_path / "card.jsonl").read_text().splitlines()
    ]
    assert [(r["seq"], r["kind"], r["cycles"]) for r in rows] == [
        (0, "quant", 20),
        (1, "matmul", 40),
    ]
    assert [set(r["nodes"]) for r in rows] == [{"0", "1"}, {"0", "1"}]


def test_interrupted_attempt_without_metadata_is_found_and_packed(
    tmp_path, monkeypatch
):
    host = tool("host_round", monkeypatch)
    directory = tmp_path / "traces" / "vitb.r1.kaos.multimesh_v9t8.4n"
    directory.mkdir(parents=True)
    point = {
        "workload": "vitb",
        "board": "multimesh_v9t8",
        "batch": 1,
        "sysnodes": 4,
        "path": "kaos",
        "inputs": 16,
        "blocks": 12,
    }
    (directory / "attempt.json").write_text(
        json.dumps({"point": point, "status": "card_failure"})
    )
    (directory / "run.log").write_bytes(b"partial failure\x00diagnostic")
    found = host.discover(tmp_path / "traces", "r1")
    assert found == [directory]
    # No timed executable is invoked on a failed attempt.
    monkeypatch.setattr(
        host, "run", lambda *args: pytest.fail("failed attempt must not be modeled")
    )
    name, complete = host.post(directory, tmp_path / "packed", tmp_path / "tables")
    assert not complete
    packed = tmp_path / "packed" / name
    assert (
        gzip.decompress((packed / "raw/run.log.gz").read_bytes())
        == b"partial failure\x00diagnostic"
    )
    assert (packed / "meta.json").exists()


def test_tools_only_change_selects_perf_stage(monkeypatch):
    runner_path = pathlib.Path(__file__).resolve().parents[1] / "run.py"
    spec = importlib.util.spec_from_file_location(
        "perf_stage_witness_runner", runner_path
    )
    assert spec and spec.loader
    runner = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, runner)
    spec.loader.exec_module(runner)
    assert "perf" in {
        s.name for s in runner.selected(["../measurements/tools/card_round.py"])
    }
