"""Retained callback interpreter on Rust tables, against frozen callback logs."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from kohakuaccel.simulation.package import Interpreter
from kohakutpu.language import machine as MF
from kohakutpu.simulation.package import Reader

GOLDEN = Path(__file__).resolve().parents[2] / "rust/golden"
SPEC = importlib.util.spec_from_file_location("interp_env", GOLDEN / "interp_env.py")
ENV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENV)
CASES = json.loads((GOLDEN / "bindings/interp.json").read_text(encoding="utf-8"))[
    "cases"
]


class Unbounded(Exception):
    pass


@pytest.mark.parametrize("case", CASES, ids=[c["case"] for c in CASES])
def test_callback_logs(case):
    world = ENV.World()
    text = (MF.MACHINES / f"{case['machine']}.json").read_text(encoding="utf-8")
    interp = Interpreter(world, reader=Reader(text), **world.callbacks())
    inp = case["input"]
    lines = 0

    def budget(frame, event, arg):
        nonlocal lines
        if event == "line":
            lines += 1
            if lines > 3_000_000:
                raise Unbounded
        return budget

    previous = sys.gettrace()
    try:
        if "unbounded" in case["output"]:
            sys.settrace(budget)
        got = ENV.run(world, interp, bytes.fromhex(inp["package"]), inp["bindings"])
    except Unbounded:
        got = {"unbounded": "no callback in 3000000 interpreter lines"}
    finally:
        sys.settrace(previous)
    assert got == case["output"]
