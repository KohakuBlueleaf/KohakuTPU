"""Parts-first controls. These are source witnesses, not recorded test results.

Planner visits are deliberately not called total compiler work; the separate
sequence-proof/work witness must meter production ordering and verification.
"""

import json
from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime.graph import split as split_module
from kohakutpu.application.runtime.graph.limits import PackageUsage
from kohakutpu.application.runtime.graph.native import NativeLaunch
from kohakutpu.application.runtime.graph.part_plan import LaunchUsage, plan_parts
from kohakutpu.application.runtime.graph.parts import JoinedSource

MACHINE = {
    "fw": {"max_pkg_units": 8, "engine_units": 8, "max_units": 8, "max_buffers": 4096}
}


def usage(steps, *, units=(1,), payloads=0, buffers=0):
    return LaunchUsage(
        PackageUsage(len(units), buffers, steps, 0, payloads), frozenset(units)
    )


def test_planner_uses_the_real_u16_limit_before_composition():
    # Eight such eager packages have 65536 steps, before any join overhead.
    for count, wanted in [
        (8, ((0, 7), (7, 8))),
        (32, ((0, 7), (7, 14), (14, 21), (21, 28), (28, 32))),
    ]:
        planned = plan_parts([{0: usage(8192)} for _ in range(count)], MACHINE)
        assert planned.intervals == wanted
        assert planned.visits <= 2 * count
        assert [i for lo, hi in planned.intervals for i in range(lo, hi)] == list(
            range(count)
        )


def test_units_are_a_union_and_payload_bytes_are_capacity_accounted():
    planned = plan_parts([{0: usage(10, units=range(4))}] * 32, MACHINE)
    assert planned.intervals == ((0, 32),)
    # The u32 payload-count limit is not enough: total encoded bytes are u32 too.
    planned = plan_parts([{0: usage(1, payloads=1 << 26)}] * 2, MACHINE)
    assert planned.intervals == ((0, 1), (1, 2))


def test_one_launch_limit_and_missing_usage_never_speculate_whole():
    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="positive"):
            plan_parts([{0: usage(1)}], MACHINE, max_launches=bad)
    planned = plan_parts([{0: usage(1)}] * 3, MACHINE, max_launches=1)
    assert planned.intervals == ((0, 1), (1, 2), (2, 3))
    planned = plan_parts(
        [{0: usage(1)}, None, {0: usage(1)}], MACHINE, participants=(0, 1)
    )
    assert planned.intervals == ((0, 1), (1, 2), (2, 3))
    assert planned.participants == (0, 1)


def test_join_steps_count_all_original_participants():
    # Coordinator costs P controls, each other node two. All are present even
    # where the eager launch did no work. Final-part closure is reserved too.
    planned = plan_parts(
        [{0: usage(65532)}, {1: usage(1)}], MACHINE, participants=(0, 1, 2, 3)
    )
    assert planned.intervals == ((0, 1), (1, 2))


def test_nonterminal_single_launch_requires_the_complete_release():
    text = (
        "fn captured_split.l1()\n"
        "    node 0\n"
        "        barrier\n"
        "        wait slot 9 n=1 consume\n"
        "        ring mesh=1 slot=9\n"
        "        # retained-cut 0\n"
        "    node 1\n"
        "        barrier\n"
        "        ring mesh=0 slot=9\n"
        "        wait slot 9 n=1 consume\n"
        "        # retained-cut 0\n"
    )
    source = JoinedSource.read(text, trailing_join=True)
    assert source.count == 1 and source.nodes == (0, 1)
    # Delete both ends, so token balance alone cannot detect the missing edge.
    broken = text.replace("        ring mesh=1 slot=9\n", "").replace(
        "        wait slot 9 n=1 consume\n        # retained-cut 0\n",
        "        # retained-cut 0\n",
    )
    with pytest.raises(ValueError, match="gather|release"):
        JoinedSource.read(broken, trailing_join=True)


def test_runtime_never_passes_the_oversized_launch_list_to_compose(monkeypatch):
    calls = []
    graph = dict(
        format="ktpu.graph",
        version=1,
        target="test",
        machine={},
        consts={},
        ports=[],
        scratch=[],
        body=[],
    )
    dev = SimpleNamespace(
        card=SimpleNamespace(machine_json=json.dumps(MACHINE), nodes=(0,)),
        machine=object(),
        home=0,
        mmu=SimpleNamespace(resolve=lambda at: (None, at)),
        _retain=lambda values, pinned=False: [],
        _node_of=lambda node: 0,
    )

    def compose(graph_json, machine, bindings, **options):
        calls.append(options)
        # This is the production compose entry spy, not a lowered-source cut.
        assert len(options["launches"]) <= 7
        return {"events": []}

    monkeypatch.setattr(split_module.rs._rs, "compile_split_graph", compose)
    monkeypatch.setattr(
        split_module.rs, "machine_json", lambda machine: json.dumps(MACHINE)
    )
    monkeypatch.setattr(
        split_module.generated,
        "from_output",
        lambda *args: SimpleNamespace(name="captured_split", scratch=[]),
    )
    for count in (8, 32):
        calls.clear()
        packets = [
            NativeLaunch(graph, {}, "dsl", package_usage={0: usage(8192)})
            for _ in range(count)
        ]
        prepared = split_module.SplitRunner(dev).prepare(packets, [()] * count)
        assert sum(len(call["launches"]) for call in calls) == count
        assert all(call["launch_count"] == count for call in calls)
        assert [call["part_first"] for call in calls] == [
            lo for lo, _ in prepared.part_intervals
        ]
        assert len(prepared.planned_parts) == len(calls)
        assert prepared.part_plan.visits <= 2 * count


def test_fitting_runtime_keeps_the_original_compose_entry(monkeypatch):
    calls = []
    graph = dict(
        format="ktpu.graph",
        version=1,
        target="test",
        machine={},
        consts={},
        ports=[],
        scratch=[],
        body=[],
    )
    dev = SimpleNamespace(
        card=SimpleNamespace(machine_json=json.dumps(MACHINE), nodes=(0,)),
        machine=object(),
        home=0,
        mmu=SimpleNamespace(resolve=lambda at: (None, at)),
        _retain=lambda values, pinned=False: [],
        _node_of=lambda node: 0,
    )
    monkeypatch.setattr(
        split_module.rs._rs,
        "compile_split_graph",
        lambda *args, **kw: calls.append(kw) or {"events": []},
    )
    monkeypatch.setattr(
        split_module.rs, "machine_json", lambda machine: json.dumps(MACHINE)
    )
    monkeypatch.setattr(
        split_module.generated,
        "from_output",
        lambda *args: SimpleNamespace(name="captured_split", scratch=[]),
    )
    packets = [NativeLaunch(graph, {}, "dsl", package_usage={0: usage(10)})] * 8
    prepared = split_module.SplitRunner(dev).prepare(packets, [()] * 8)
    assert len(calls) == 1 and len(calls[0]["launches"]) == 8
    assert "part_nodes" not in calls[0] and "launch_count" not in calls[0]
    assert prepared.planned_parts == ()
