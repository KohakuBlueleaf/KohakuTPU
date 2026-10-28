"""Retained runner registration and offline package preparation; no target opens."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from harness import vectors
from harness.registry import MUTANTS, RUNNERS
from runners.target import isa, l0

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

NAMES = {
    "isa.card.example",
    "isa.card.kernel",
    "isa.bench",
    "l0.card.boot",
    "l0.card.example",
    "l0.card.package",
    "l0.bench",
    "l1.card",
    "l2.card",
    "l2.verilated",
    "num.card.dsl",
    "num.card.elementwise",
    "num.card.example",
    "num.card.linear",
    "num.card.product",
    "num.card.rowsum",
    "num.card.tinygrad",
}
VECTORS = [v for v in vectors.load() if v["runner"] in NAMES and v["tier"] > 0]


@pytest.mark.parametrize("name", sorted(NAMES))
def test_retained_runner_is_registered_from_target_module(name):
    assert RUNNERS[name].__module__.startswith("runners.target.")


def test_target_modules_import_no_python_compiler():
    for file in Path(isa.__file__).parent.glob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(("kohakutpu.compiler", "kohakuaccel.compiler")):
                    assert module == "kohakutpu.compiler" and [
                        n.name for n in node.names
                    ] == ["_rs"], file
            if isinstance(node, ast.Import):
                assert not any(
                    n.name.startswith(("kohakutpu.compiler", "kohakuaccel.compiler"))
                    for n in node.names
                ), file


def test_bench_mutants_reach_the_moved_runners():
    with MUTANTS["isa.bench/short"]():
        assert isa.XSIM_ARGS == ["--max-time", "1ns"]
    assert isa.XSIM_ARGS == []
    for key, value in (("neg_go", "NEG_GO"), ("neg_wait", "NEG_WAIT")):
        with MUTANTS[f"l0.bench/{key}"]():
            assert l0.VLT_DEFINES == (value,)
        assert l0.VLT_DEFINES == ()


@pytest.mark.parametrize(
    "v", [v for v in VECTORS if v["runner"] == "l0.card.package"], ids=lambda v: v.id
)
def test_package_vectors_build_offline(v):
    spec = machine_of(v["machine"])
    card = SimpleNamespace(
        machine=spec, machine_json=spec.machine_json, nodes=[0], fetch=spec.fetch
    )
    inp = v["input"]
    addrs = {
        name: 0x400000 + n * 0x100000 for n, name in enumerate(inp.get("buffers", {}))
    }
    raw = l0.build_package(card, MF.load(v["machine"]), inp, addrs)
    tables = _rs.package_read(raw, spec.machine_json)
    if inp.get("reorder"):
        assert _rs.package_read(l0.reorder(raw), spec.machine_json) == tables
