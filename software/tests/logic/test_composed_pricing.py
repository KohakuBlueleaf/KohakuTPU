"""Admission prices a call from its scheduled L1: the price a card build gives."""

import json

import pytest

from kohakutpu.application.runtime.machine import machine_of
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
    assert price == _rs.l1_cycles(built["l1"], built["name"], machine)
