"""The Rust stack through `kohakutpu.compiler._rs` against the Python tracer:
a DSL kernel's graph turns into the L2 text the Python tracer prints."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kohakutpu.compiler import _rs
from kohakutpu.language import dsl as kt
from kohakutpu.language import machine as MF
from kohakutpu.language.examples import dsl as ex

BOARD = "multimesh_v9t6"


def machine_text() -> str:
    return (MF.MACHINES / f"{BOARD}.json").read_text(encoding="utf-8")


def traced(name: str):
    m = MF.load(BOARD)
    tm = kt.Machine.of(m)
    e = ex.EXAMPLES[name]
    return e.kernel.trace(e.given, machine=tm, **e.consts)


@pytest.mark.parametrize("name", ["activation", "softmax", "linear"])
def test_rust_graph_stage_prints_the_python_l2(name):
    t = traced(name)
    assert _rs.l2_of(json.dumps(t.graph), machine_text()) == t.text


def test_another_graph_gives_another_l2():
    a, b = traced("activation"), traced("softmax")
    assert _rs.l2_of(json.dumps(b.graph), machine_text()) != a.text


def test_a_refusal_names_its_stage_and_clause():
    with pytest.raises(_rs.Refused) as got:
        _rs.compile("not an l2 module\n", machine_text(), None, "")
    assert got.value.stage == "passes"
    assert got.value.clause


def test_the_build_names_its_machine_schema():
    assert _rs.MACHINE_SCHEMA == json.loads(machine_text())["schema"]


def _import_with(machines: Path, code: str, **env) -> subprocess.CompletedProcess:
    """`code` run in a fresh interpreter with the Rust stack reading machine
    files from `machines`."""
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "KTPU_MACHINES": str(machines), **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_build_that_cannot_read_the_machine_files_refuses_to_import(tmp_path):
    doc = json.loads(machine_text())
    (tmp_path / "good.json").write_text(json.dumps(doc), encoding="utf-8")
    doc["machine"]["mg"]["zz_new_fact"] = 1
    (tmp_path / "newer.json").write_text(json.dumps(doc), encoding="utf-8")
    got = _import_with(tmp_path, "from kohakutpu.compiler import _rs")
    assert got.returncode != 0
    assert "ImportError" in got.stderr
    assert "newer: " in got.stderr and "zz_new_fact" in got.stderr, got.stderr
    assert "scripts/py/build_rs.py" in got.stderr and "good:" not in got.stderr
    loud = _import_with(
        tmp_path,
        "from kohakutpu.stack import rs",
    )
    assert "ImportError" in loud.stderr, loud.stderr
    assert "zz_new_fact" in loud.stderr, "the rebuild message is the cause"
