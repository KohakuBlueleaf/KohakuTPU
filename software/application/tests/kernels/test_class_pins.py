"""The class routes' graphs pinned by library class over `class_calls.CALLS`,
each pin the graph that route's Python kernel traced."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime import generated as G
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import rs

sys.path.insert(0, str(Path(__file__).parent))
import class_calls as CC

MACHINE = machine_of("vlt_v9t6_2n")
KEYS = {
    "rows": "661ef290e570ddb3",
    "rows_offset": "04cc24c3921a67c2",
    "rows_f32": "4481b9cccc20b7cd",
    "rows_stat": "9de4b23a5c261a5f",
    "rows_prod": "643505e80b63fd33",
    "rows_row": "9ff6232b91cb9e7f",
    "rows_col": "843768a7f871da98",
    "rows_scalar": "3196e721b5838b35",
    "wide": "c8185e6e3c15de3d",
    "wide_col": "69f7617f2df09d63",
    "wide_row": "2da39011f2f50680",
    "wide_scalar": "d0e181e2124e06b7",
    "fold": "2ed445bd8880aefe",
    "flat": "f354a352b1230689",
    "copy": "add26cd39036d5c8",
    "walk": "769bae519c021a8f",
    "transpose": "4cef9d16a6297cdf",
    "quant": "e7694bb06eec7332",
    "mm": "7678247607d036e1",
    "direct": "92facb2e5efd0588",
    "band_1": "43489ae7b48a48ba",
    "wide_256": "447141695f19354d",
    "wide_direct": "a01f7a28fd88e464",
    "lanes": "e4fe49d7cf87bd98",
    "lanes_keep": "0d2dd8195dcb5b99",
}


@pytest.mark.parametrize("name", list(CC.CALLS))
def test_class_call_keeps_its_graph(name, monkeypatch):
    generator, arguments, environment = CC.CALLS[name]
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)
    shape_class = None if generator == "lanes.class_kernel" else CC.CLASS
    source, _ = G.classed(
        SimpleNamespace(machine=MACHINE), generator, arguments, shape_class
    )
    key = json.loads(rs.library_key(source, rs.machine_json(MACHINE)))["class"]
    assert key[:16] == KEYS[name]
