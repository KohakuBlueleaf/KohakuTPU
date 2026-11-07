"""Admission prices a call from its scheduled L1: the price a card build gives."""

import json

import pytest

from kohakutpu.application.kernels.template import GraphKernel
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
