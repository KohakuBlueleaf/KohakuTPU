"""The traced graph (`Traced.graph`, DSL.GRAPH): plain JSON holding every
statement the L2 text prints, each with its structure."""

import importlib
import json
import math
import pathlib

import pytest

import kohakutpu.language.dsl as kt
from kohakutpu.language import machine as MF
from kohakutpu.language.dsl import errors as E
from kohakutpu.language.dsl import graph as G
from kohakutpu.language.examples import dsl as ex

MACHINE = kt.Machine.of(MF.load("multimesh_v9t8"))

#: The kernel module (`dsl.kernel` is the decorator).
KM = importlib.import_module("kohakutpu.language.dsl.kernel")


def stmts(block: list) -> list:
    out = []
    for s in block:
        out.append(s)
        out += stmts(s.get("body") or [])
    return out


def text_lines(text: str) -> list:
    """The body lines after the `fn` line, stripped, scratch buffers dropped."""
    body = text.split("\n")[3:-1]
    return [ln.strip() for ln in body if " = buffer " not in ln]


@pytest.mark.parametrize("name", sorted(ex.EXAMPLES))
def test_each_example_graph_is_json_and_holds_every_statement(name):
    e = ex.EXAMPLES[name]
    t = e.kernel.trace(e.given, machine=MACHINE, **e.consts)
    g = json.loads(json.dumps(t.graph, allow_nan=False))
    assert (g["format"], g["version"], g["name"], g["target"]) == (
        G.FORMAT,
        G.VERSION,
        t.name,
        "ktpu.v9",
    )
    assert [p["name"] for p in g["ports"]] == [n for n, _ in t.params]
    assert [p["role"] for p in g["ports"]] == [t.roles[n] for n, _ in t.params]
    every = stmts(g["body"])
    lines = text_lines(t.text)
    assert len(every) == len(lines)
    for s, ln in zip(every, lines):
        head = ln.split("  :")[0]
        if s["kind"] == "value":
            assert head.startswith(f"{s['target']} = {s['op']}"), (s, ln)
        else:
            op = {"sticky": "if", "mover": s.get("op")}.get(s["kind"], s["kind"])
            assert head.startswith(f"{op} "), (s, ln)
        assert s["where"]


def test_kernel_graph_is_the_traces():
    e = ex.EXAMPLES["softmax"]
    assert (
        e.kernel.graph(e.given, machine=MACHINE, **e.consts)
        == e.kernel.trace(e.given, machine=MACHINE, **e.consts).graph
    )


@kt.kernel
def _shapes(x=kt.In(64, 128), y=kt.Out(64, 128)):
    kt.copy(y, x, walk=[(2, 4096), (32, -128)], at=64)
    kt.fill_value(y, 0, ew=16)
    for c in kt.par(2, on=kt.vc):
        for t in kt.range(8):
            r = c * 32 + t * 4
            v = kt.load(x[r : r + 4, :])
            kt.store(y[r : r + 4, :], v * math.inf + kt.rmax(v))


def test_structure_of_indices_operands_and_attributes():
    t = _shapes.trace({"x": ((64, 128), "f16")}, machine=MACHINE)
    g = json.loads(json.dumps(t.graph, allow_nan=False))
    copy, fill, par = g["body"]
    assert copy["kw"] == [
        ["walk", {"walk": [[2, 4096], [32, -128]]}],
        ["at", {"int": 64}],
    ]
    assert "walk=((2, 4096), (32, -128)) at=64" in t.text
    assert fill["kw"] == [["value", {"int": 0}], ["ew", {"int": 16}]]
    assert par["unit"] == {
        "kind": "vc",
        "index": {"const": 0, "terms": [[{"var": "c1"}, 1]]},
    }
    load = par["body"][0]["body"][0]
    assert load["args"] == [
        {
            "view": {
                "buffer": "x",
                "axes": [
                    {
                        "run": [
                            {
                                "const": 0,
                                "terms": [[{"var": "c1"}, 32], [{"var": "t1"}, 4]],
                            },
                            4,
                        ]
                    },
                    "all",
                ],
            }
        }
    ]
    ops = {s.get("op"): s for s in stmts(g["body"])}
    assert ops["mul"]["args"] == [{"value": "v1"}, {"float": "inf"}]
    assert ops["add"]["args"] == [{"value": "e1"}, {"bcast": "s1"}]
    assert "mul v1, inf" in t.text and "add e1, s1[:, *]" in t.text


@kt.kernel
def _blocks(x=kt.In(64, 128), y=kt.Out(64, 128)):
    for c in kt.par(2, on=kt.vc):
        with kt.when(c == 0):
            for t in kt.range(16):
                v = kt.unpack(kt.load(x[t * 4 : t * 4 + 4, :]))
                e = kt.exp2d(v, v)
                with kt.sticky():
                    e.assign(e * 2.0)
                kt.store(y[t * 4 : t * 4 + 4, :], e)


#: The lines of `with kt.when` and `with kt.sticky` in `_blocks` (the
#: code's first line is the decorator's).
WHEN_LINE = _blocks.fn.__code__.co_firstlineno + 3
STICKY_LINE = WHEN_LINE + 4


def _block_wheres() -> list:
    E._inside.cache_clear()
    KM._TRACES.clear()
    _blocks._cache.clear()
    g = _blocks.trace({"x": ((64, 128), "f16")}, machine=MACHINE).graph
    return [s["where"] for s in stmts(g["body"]) if s["kind"] in ("when", "sticky")]


def test_when_and_sticky_point_at_the_authors_line():
    here = pathlib.Path(__file__).name
    assert _block_wheres() == [f"{here}:{WHEN_LINE}", f"{here}:{STICKY_LINE}"]


def test_without_the_contextlib_skip_the_line_is_contextlibs(monkeypatch):
    """The witness: the check above fails when the skip is removed."""
    monkeypatch.setattr(E, "FOREIGN", ())
    try:
        assert all(w.startswith("contextlib.py:") for w in _block_wheres())
    finally:
        monkeypatch.undo()
        E._inside.cache_clear()


def test_a_rebound_name_is_the_graphs_target():
    @kt.kernel
    def k(x=kt.In(64, 128), y=kt.Out(64, 128)):
        for c in kt.par(1, on=kt.vc):
            for t in kt.range(16):
                v = kt.unpack(kt.load(x[t * 4 : t * 4 + 4, :]))
                e = kt.exp2d(v, v)
                with kt.sticky():
                    e.assign(e * 2.0)
                kt.store(y[t * 4 : t * 4 + 4, :], e)

    g = k.trace({"x": ((64, 128), "f16")}, machine=MACHINE).graph
    sticky = next(s for s in stmts(g["body"]) if s["kind"] == "sticky")
    assert sticky["body"][0]["target"] == "e1"
