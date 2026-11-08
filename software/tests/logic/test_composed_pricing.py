"""Admission prices a call from its scheduled L1: the price a card build gives."""

import json
from types import SimpleNamespace

import pytest

from kohakutpu.application.kernels.template import GraphKernel
from kohakutpu.application.runtime import forms
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack.rs import _rs

PLAN = {
    "made_by": {
        "gen": "relayout.plan",
        "args": {
            "walk": [[8, 64], [2, 0], [64, 1]],
            "at": 0,
            "dwalk": [[8, 128], [2, 64], [64, 1]],
            "dat": 0,
            "m": 1024,
            "src_n": 512,
            "keep": False,
            "z0": 0,
            "tree": True,
        },
    }
}
ROWS = {
    "e": {"nodes": [{"load": 0}, {"reduce": "sum", "arg": 0}], "root": 1},
    "operands": [{"kind": "full", "dtype": "f16", "offset": 0, "pitch": 64, "rows": 0}],
    "R": 16,
    "W": 64,
    "out": {"kind": "row", "dtype": "f16", "offset": 0, "pitch": 0, "rows": 0},
    "made": {"0": [PLAN]},
    "alias": {},
}


@pytest.mark.parametrize(
    "call",
    [
        {"gen": "prepared.rows_kernel", "args": {**ROWS, "composed": [0]}},
        {"gen": "prepared.rows_kernel", "args": {**ROWS, "composed": []}},
        {"gen": "relayout.relayout_kernel", "args": {"pl": PLAN}},
    ],
    ids=["composed", "materialized", "relayout"],
)
def test_a_price_is_the_card_builds_cycles(call):
    machine = machine_of("multimesh_v9t8").machine_json
    price = _rs.call_cycles(json.dumps(call), machine)
    graph = _rs.generate(call["gen"], json.dumps(call["args"]), machine)
    built = _rs.compile(graph, machine, fetch=True)
    assert price is not None
    assert price == _rs.graph_cycles(graph, machine)
    assert price == _rs.l1_cycles(built["l1"], built["name"], machine)


def test_graph_compile_cache_distinguishes_programs_with_the_same_display_text():
    machine = machine_of("multimesh_v9t8")
    args = {"src_rows": 8, "dst_rows": 4, "width": 32, "rows": 4, "initialize": False}
    traces = []
    for origin in (0, 4):
        source = json.loads(
            _rs.generate(
                "convert.prefix_kernel",
                json.dumps({**args, "src_first": origin}),
                machine.machine_json,
            )
        )
        source["name"] = "cached_input_prefix"
        traces.append(GraphKernel(source).trace(machine=K.dsl_machine(machine)))
    first, second = (K.compile(trace, machine=machine) for trace in traces)
    expected = _rs.compile(traces[1].graph_json, machine.machine_json, fetch=True)
    assert first.l1_text != second.l1_text
    assert second.l1_text == expected["l1"]
    assert K.compile(traces[0], machine=machine) is first
    assert K.compile(traces[1], machine=machine) is second


def test_sequence_decisions_follow_the_launch_grant_and_family_policy(monkeypatch):
    machine = machine_of("multimesh_v9t8")
    dev = SimpleNamespace(machine=machine, home=None, card=SimpleNamespace(nodes=(0,)))
    monkeypatch.setattr(forms, "COMPOSED", "cost")
    monkeypatch.setenv("KTPU_SPLIT_CONVERT_WALK_KERNEL_OPTION", "p1")
    monkeypatch.delenv("KTPU_SPLIT_CONVERT_COPY_KERNEL_OPTION", raising=False)
    nbytes = 262144
    read = {
        "gen": "convert.walk_kernel",
        "args": {
            "src_bytes": nbytes,
            "levels": [[nbytes // 32, 32]],
            "at": 0,
            "dst_bytes": nbytes,
            "dlevels": [[nbytes // 32, 32]],
            "dat": 0,
        },
    }
    materialized = [{"gen": "convert.copy_kernel", "args": {"nbytes": nbytes}}]
    assert forms.composed(dev, read, materialized)
    dev.card.nodes = (3, 1, 0, 2)
    assert not forms.composed(dev, read, materialized)
    dev.home = 3
    assert forms.composed(dev, read, materialized)
    dev.home = None
    assert not forms.composed(dev, read, materialized)
    monkeypatch.delenv("KTPU_SPLIT_CONVERT_WALK_KERNEL_OPTION")
    assert forms.composed(dev, read, materialized)
    dev.card.nodes = (0,)
    assert forms.composed(dev, read, materialized)


def test_admission_supplies_the_emitted_plan_without_replanning():
    machine = machine_of("multimesh_v9t8").machine_json
    call = {"gen": "convert.copy_kernel", "args": {"nbytes": 7360}}
    nodes = [3, 1, 0, 2]
    encoded = json.dumps(call)
    assert _rs.read_composed("cost", encoded, f"[{encoded}]", machine, nodes=nodes)
    admitted = _rs.bind_cache_stats()
    _rs.generate_split(call["gen"], json.dumps(call["args"]), machine, nodes)
    emitted = _rs.bind_cache_stats()
    assert emitted["split_plans"] == admitted["split_plans"]
    assert emitted["split_plan_hits"] > admitted["split_plan_hits"]

    other_nodes = list(reversed(nodes))
    _rs.generate_split(call["gen"], json.dumps(call["args"]), machine, other_nodes)
    changed = _rs.bind_cache_stats()
    assert changed["split_plans"] > emitted["split_plans"]
    _rs.generate_split(call["gen"], json.dumps(call["args"]), machine, nodes)
    assert _rs.bind_cache_stats()["split_plans"] == changed["split_plans"]
