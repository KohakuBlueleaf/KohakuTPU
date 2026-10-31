"""A fused group's kernel (`fused.kernel`, `launch_emit.flush_multi_stage`):
its graph pinned by hash over the shapes a group takes, each pin the graph
the group's Python emitter traced."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import launch_emit as LE
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated as G
from kohakutpu.stack import rs

BOARD = "vlt_v9t6s_1n"
NEG = X.op("neg", X.Load(0))
ADD = X.op("add", X.Load(0), X.Load(1))
TWICE = X.op("mul", X.Load(0), 2.0)


def full(pitch):
    return Operand("full", pitch=pitch)


def rows(e, operands, R, W, out, binds):
    return ("rows", e, operands, R, W, out, binds, {})


def flat(e, dtypes, n, binds, lanes=()):
    return ("flat", e, dtypes, n, "f16", lanes, binds, {})


#: name -> the stages of a group.
CASES = {
    "two_rows": [
        rows(NEG, [full(128)], 32, 128, full(128), dict(in0=1, out=2)),
        rows(
            ADD, [full(128), full(128)], 32, 128, full(128), dict(in0=1, in1=3, out=4)
        ),
    ],
    "two_rows_tail": [
        rows(NEG, [full(128)], 34, 128, full(128), dict(in0=1, out=2)),
        rows(TWICE, [full(128)], 34, 128, full(128), dict(in0=3, out=4)),
    ],
    "two_rows_wide": [
        rows(NEG, [full(1024)], 32, 1024, full(1024), dict(in0=1, out=2)),
        rows(TWICE, [full(1024)], 32, 1024, full(1024), dict(in0=3, out=4)),
    ],
    "two_rows_stat": [
        rows(
            ADD,
            [full(128), Operand("row")],
            32,
            128,
            full(128),
            dict(in0=1, in1=5, out=2),
        ),
        rows(
            ADD,
            [full(128), Operand("scalar", offset=3)],
            32,
            128,
            full(128),
            dict(in0=3, in1=6, out=4),
        ),
    ],
    "two_rows_col": [
        rows(
            ADD,
            [full(128), Operand("col")],
            32,
            128,
            full(128),
            dict(in0=1, in1=5, out=2),
        ),
        rows(NEG, [full(128)], 32, 128, full(128), dict(in0=3, out=4)),
    ],
    "rows_then_dependent": [
        rows(NEG, [full(128)], 32, 128, full(128), dict(in0=1, out=2)),
        rows(NEG, [full(128)], 32, 128, full(128), dict(in0=2, out=3)),
    ],
    "rows_row_out": [
        rows(
            X.Reduce("sum", X.Load(0)),
            [full(128)],
            32,
            128,
            Operand("row"),
            dict(in0=1, out=2),
        ),
        rows(NEG, [full(128)], 32, 128, full(128), dict(in0=3, out=4)),
    ],
    "rows_tail_then_copy": [
        rows(NEG, [full(128)], 35, 128, full(128), dict(in0=1, out=2)),
        ("copy", 256, 2, 7, {}),
    ],
    "flat_copy_folds": [
        flat(NEG, ("f16",), 256, dict(in0=1, out=2)),
        ("copy", 512, 1, 5, {}),
    ],
    "flat_lanes": [
        flat(ADD, ("f16", "f16"), 256, dict(in0=1, in1=3, out=2), lanes=((1, 5),)),
        flat(NEG, ("f16",), 256, dict(in0=4, out=6)),
    ],
    "flat_staged": [
        flat(NEG, ("f16",), 130, dict(in0=1, out=2)),
        flat(TWICE, ("f16",), 130, dict(in0=3, out=4)),
    ],
    "flat_f32": [
        flat(NEG, ("f32",), 256, dict(in0=1, out=2)),
        flat(TWICE, ("f32",), 256, dict(in0=3, out=4)),
    ],
    "flat_many": [
        flat(NEG, ("f16",), 8192, dict(in0=1, out=2)),
        flat(TWICE, ("f16",), 8192, dict(in0=3, out=4)),
        flat(ADD, ("f16", "f16"), 8192, dict(in0=5, in1=6, out=7)),
    ],
    "flat_then_rows": [
        flat(NEG, ("f16",), 256, dict(in0=1, out=2)),
        rows(NEG, [full(128)], 32, 128, full(128), dict(in0=2, out=3)),
        ("copy", 64, 3, 9, {}),
    ],
}


def bare(node):
    if isinstance(node, dict):
        return {k: bare(v) for k, v in node.items() if k != "where"}
    if isinstance(node, list):
        return [bare(v) for v in node]
    return node


@pytest.fixture(scope="module")
def dev():
    device = D.Device("offline", board=BOARD)
    yield device
    device.close()


def package(dev, stages, monkeypatch) -> tuple:
    """The generator arguments and bindings `flush_multi_stage` launches."""
    got = []

    def launch(_dev, generator, arguments, bindings, **_):
        got.append((generator, arguments, bindings))
        return "run"

    monkeypatch.setattr(G, "launch", launch)
    scope = SimpleNamespace(dev=dev, stages=list(stages), flushed=False, info={})
    assert LE.flush_multi_stage(scope) == ["run"]
    ((generator, arguments, bindings),) = got
    assert generator == "fused.kernel"
    return json.dumps(arguments, sort_keys=True), bindings


def graph_hash(dev, arguments) -> str:
    """The sha256 head of the generator's graph, `where` aside."""
    got = rs._rs.generate("fused.kernel", arguments, rs.machine_json(dev.machine))
    text = json.dumps(bare(json.loads(got)), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


#: name -> `graph_hash` on `BOARD`.
GRAPHS = {
    "flat_copy_folds": "40e72ea56ffc4eac",
    "flat_f32": "99ee9cc0f6e674fd",
    "flat_lanes": "c25bb44d7217fa35",
    "flat_many": "b942ec8a3e06c0ce",
    "flat_staged": "e2cacb0f20d73afc",
    "flat_then_rows": "f60f1ff39e96f751",
    "rows_row_out": "f028ae79f2e3b586",
    "rows_tail_then_copy": "418aefd70863d9aa",
    "rows_then_dependent": "b262869374483ca6",
    "two_rows": "b0907b1c07dc6b5c",
    "two_rows_col": "a4673eed3166522b",
    "two_rows_stat": "4b874977480b9472",
    "two_rows_tail": "fdda836a26245fce",
    "two_rows_wide": "36e106364ec3c4d2",
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_group_of_stages_is_pinned(dev, name, monkeypatch):
    arguments, _ = package(dev, CASES[name], monkeypatch)
    assert graph_hash(dev, arguments) == GRAPHS[name]


def test_ports_bind_each_address_once(dev, monkeypatch):
    """Two members over one input read one port; a staged flat stage binds
    its port, not its scratch."""
    _, binds = package(dev, CASES["two_rows"], monkeypatch)
    assert sorted(binds.values()) == [1, 2, 3, 4]
    _, binds = package(dev, CASES["flat_staged"], monkeypatch)
    assert sorted(binds.values()) == [1, 2, 3, 4]
