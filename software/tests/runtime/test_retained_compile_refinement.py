"""Compile-time capacity failures refine only a preplanned launch interval."""

import json
from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime.graph import split as split_module
from kohakutpu.application.runtime.graph.limits import PackageUsage
from kohakutpu.application.runtime.graph.native import NativeLaunch
from kohakutpu.application.runtime.graph.part_plan import LaunchUsage

MACHINE = {
    "fw": {"max_pkg_units": 8, "engine_units": 8, "max_units": 8, "max_buffers": 4096}
}


def setup(monkeypatch, references=()):
    dev = SimpleNamespace(
        card=SimpleNamespace(machine_json=json.dumps(MACHINE), nodes=(0,)),
        machine=object(),
        home=0,
        mmu=SimpleNamespace(resolve=lambda at: (None, at)),
        _retain=lambda values, pinned=False: list(references),
        _node_of=lambda node: 0,
    )
    graph = {
        "format": "ktpu.graph",
        "version": 1,
        "target": "test",
        "machine": {},
        "consts": {},
        "ports": [],
        "scratch": [],
        "body": [],
    }
    packet = NativeLaunch(
        graph,
        {},
        "dsl",
        package_usage={0: LaunchUsage(PackageUsage(1, 0, 10, 0, 0), frozenset({1}))},
    )
    monkeypatch.setattr(split_module.rs, "machine_json", lambda _: json.dumps(MACHINE))
    monkeypatch.setattr(
        split_module.generated,
        "from_output",
        lambda *args: SimpleNamespace(name="captured_split", scratch=[]),
    )
    return dev, packet


@pytest.mark.parametrize("failure", ["L0.PKG.011", "RUL.005"])
def test_compile_capacity_refines_only_failed_planned_interval(monkeypatch, failure):
    dev, packet = setup(monkeypatch)
    calls = []

    def compose(*args, **kw):
        first, count = kw["part_first"], len(kw["launches"])
        calls.append((first, first + count))
        assert kw["launch_count"] == 8
        if first == 0 and count > 2:
            raise ValueError(f"{failure}: exact writer capacity")
        return {"events": [{"first": first}]}

    monkeypatch.setattr(split_module.rs._rs, "compile_split_graph", compose)
    prepared = split_module.SplitRunner(dev, max_part_launches=4).prepare(
        [packet] * 8, [()] * 8
    )
    assert calls == [(0, 4), (0, 2), (2, 4), (4, 8)]
    assert prepared.part_intervals == ((0, 2), (2, 4), (4, 8))
    assert prepared.part_plan.intervals == prepared.part_intervals
    assert len(prepared.planned_parts) == 3
    assert prepared.events == ({"first": 0}, {"first": 2}, {"first": 4})


@pytest.mark.parametrize("failure", ["L0.PKG.011", "unrelated compile error"])
def test_indivisible_compile_error_releases_references(monkeypatch, failure):
    released = []
    arena = SimpleNamespace(unretain=released.append)
    dev, packet = setup(monkeypatch, [(arena, 17)])
    error = ValueError(failure)
    calls = []

    def compose(*args, **kw):
        calls.append(kw)
        raise error

    monkeypatch.setattr(split_module.rs._rs, "compile_split_graph", compose)
    with pytest.raises(ValueError) as caught:
        split_module.SplitRunner(dev).prepare([packet], [()])
    assert caught.value is error
    assert len(calls) == 1
    assert released == [17]
