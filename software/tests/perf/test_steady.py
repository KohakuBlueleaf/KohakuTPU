"""Steady means observed retained replay, never 'the second input'."""

import json
from types import SimpleNamespace as NS

import pytest
import traces
from intervals import Inputs

from kohakutpu.application.runtime import device as D


def sample(index, complete=True):
    return {
        "input": index,
        "group": "input",
        "index": index,
        "complete": complete,
        "global_host_bound_ns": 1000 + index,
        "global_cycles_bound": 100 + index,
    }


def events_and_rows(phases):
    events, rows = [], []
    for index, phase in enumerate(phases):
        events.append(
            {"seq": index, "input": index, "phase": phase, "name": f"k{index}"}
        )
        rows.append(
            {
                "seq": index,
                "input": index,
                "name": f"k{index}",
                "kind": "graph",
                "cycles": 10 + index,
                "max_node_cycles": 10 + index,
            }
        )
    return events, rows


@pytest.mark.parametrize("path", ["firmware", "kaos"])
def test_capture_only_second_input_produces_no_steady_score(path):
    events, rows = events_and_rows(["ordinary", "capture"])
    assert traces.steady_inputs([sample(0), sample(1)], events, rows, path) == []


@pytest.mark.parametrize("path", ["firmware", "kaos"])
def test_only_completed_retained_replays_are_selected(path):
    phases = [
        "ordinary",
        "capture",
        "capture",
        "capture",
        "retained_replay",
        "retained_replay",
    ]
    events, rows = events_and_rows(phases)
    inputs = [sample(i, i != 5) for i in range(6)]
    selected = traces.steady_inputs(inputs, events, rows, path)
    assert [s[0]["input"] for s in selected] == [4]
    assert sum(r["max_node_cycles"] for r in selected[0][1]) == 14


def test_mixed_recapture_and_replay_input_is_not_steady():
    events, rows = events_and_rows(["retained_replay"])
    events.append({"seq": 1, "input": 0, "phase": "capture", "name": "recaptured"})
    assert not traces.steady_inputs([sample(0)], events, rows, "firmware")


def test_missing_retained_completion_cannot_qualify_an_input():
    events, rows = events_and_rows(["retained_replay"])
    rows[0]["seq"] = 99
    assert not traces.steady_inputs([sample(0)], events, rows, "kaos")


def test_eager_uses_explicit_input_boundary_not_repeated_block_names():
    events, rows = events_and_rows(["ordinary", "ordinary", "ordinary"])
    for event in events:
        event["name"] = "same_block_kernel"
    selected = traces.steady_inputs(
        [sample(i) for i in range(3)], events, rows, "eager"
    )
    assert [s[0]["input"] for s in selected] == [1, 2]


def test_summary_reports_max_sum_and_global_interval_separately(tmp_path):
    events, rows = events_and_rows(["capture", "retained_replay", "retained_replay"])
    for filename, values in (
        ("launches.jsonl", events),
        ("card.jsonl", rows),
        ("inputs.jsonl", [sample(i) for i in range(3)]),
    ):
        (tmp_path / filename).write_text("".join(json.dumps(r) + "\n" for r in values))
    summary = traces.steady_summary(tmp_path, {"path": "firmware"})
    assert summary["replay_inputs"] == 2
    assert summary["selected_inputs"] == [1, 2]
    group = summary["groups"]["input"]
    assert group["launch_max_sum"] == 12
    assert group["kinds"] == {"graph": 12}
    assert group["global_cycles_bound"] == 101.5
    assert group["global_host_bound_ns"] == 1001.5


def test_global_interval_uses_one_reference_counter_despite_different_node_epochs(
    tmp_path, monkeypatch
):
    trace = NS(out=tmp_path, count=0, current_input=None)
    observer = Inputs(trace)
    calls = []
    dev = NS(drain=lambda: calls.append("drain"))
    monkeypatch.setattr(D, "device", lambda: dev)
    before = {
        0: {"first": (10, 12), "lo": 0, "hi": 0},
        1: {"first": (14, 20), "lo": 9000, "hi": 9002},
    }
    after = {
        0: {"first": (120, 122), "lo": 0, "hi": 0},
        1: {"first": (124, 130), "lo": 9000, "hi": 9002},
    }
    values = iter((before, after))
    monkeypatch.setattr(observer, "timebase", lambda dev: next(values))
    with observer(0, "input"):
        assert trace.current_input == 0
        trace.count = 2
    observer.close()
    row = json.loads((tmp_path / "inputs.jsonl").read_text())
    assert calls == ["drain", "drain"]
    assert row["global_cycles_bound"] == 120 - 20
    assert row["reference_node"] == 0
    assert row["complete"]
    assert trace.current_input is None
