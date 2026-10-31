"""Automatic recording fusion, host boundaries, and live-owner export witnesses."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.plan.emit import expr
from kohakutpu.application.runtime.device import Device, Recorded
from kohakutpu.application.runtime.graph import recorded as NR
from kohakutpu.application.runtime.graph.native import (
    NativeLaunch,
    copy_source,
    fuse,
    port_bytes,
)
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def row_kernel(dev, expression):
    kernel, given, _ = V.rows_kernel(
        expression,
        (V.Operand("full", pitch=128),),
        16,
        128,
        V.Operand("full", pitch=128),
        1,
    )
    traced = kernel.trace(given, machine=K.dsl_machine(dev.machine))
    return dev.kernel(json.dumps(traced.graph))


def capture(dev, released=True, write=False):
    a = row_kernel(dev, X.op("add", X.Load(0), 0.125))
    b = row_kernel(dev, X.op("max", X.Load(0), 0))
    x, h, y = [dev.alloc(4096) for _ in range(3)]
    dev.record()
    dev.run(a, {"in0": x, "out": h})
    if write:
        dev.write(h, bytes(4096))
    dev.run(b, {"in0": h, "out": y})
    if released:
        dev.free(h)
    return dev.recorded(), (x, h, y)


def test_recorded_automatic_fusion_preserves_live_exports_and_host_boundaries(
    monkeypatch,
):
    monkeypatch.setenv("KTPU_GRAPH_FUSE", "1")
    dev = Device("offline", board="vlt_v9t6s_1n")
    yes, _ = capture(dev)
    assert (yes.native.before, yes.native.after, yes.native.rules) == (
        2,
        1,
        ["elementwise"],
    )
    yes.close()
    with patch.object(rs._rs, "compile", side_effect=AssertionError("compile on hit")):
        hit, _ = capture(dev)
        assert hit.native.after == 1
        hit.close()
    # Negative control: retaining an external owner prevents elision.
    exported, _ = capture(dev, released=False)
    assert exported.native.after == 2 and not exported.native.rules
    exported.close()
    boundary, _ = capture(dev, write=True)
    assert boundary.native.after == 2
    assert [kind for kind, _ in boundary.native.segments] == ["run", "write", "run"]
    boundary.close()
    dev.close()


def test_native_prepare_failure_is_transactional_and_wait_failure_quarantines_device():
    dev = Device("offline", board="vlt_v9t6s_1n")
    dev.card.queues = {0: object()}
    graph = NR.NativeRecording(
        dev,
        [("run", [b"a"]), ("write", (0x100000, b"x")), ("run", [b"b"])],
        [],
        2,
        2,
        [],
        0,
    )
    first = SimpleNamespace(close=Mock())
    with (
        patch.object(
            NR.FirmwareRunner, "prepare", side_effect=[first, MemoryError("upload")]
        ),
        pytest.raises(MemoryError),
    ):
        graph.run()
    first.close.assert_called_once()
    assert graph.prepared == [] and not graph.busy
    graph.prepared = [
        SimpleNamespace(submit=Mock(side_effect=TimeoutError("pending")), close=Mock()),
        None,
        None,
    ]
    log = Recorded([], [], dev)
    log.native = graph
    with pytest.raises(TimeoutError):
        dev.replay(log)
    with pytest.raises(RuntimeError):
        dev._healthy()
    with pytest.raises(RuntimeError, match="stop device"):
        log.close()
    assert dev._uncertain == [log]
    dev.close()
    log.close()


def test_mmeff_ring_has_bounded_stage_capacity_and_same_ports():
    machine = machine_of("vlt_v9t6s_1n").machine_json
    args = dict(
        mm={
            "made_by": {
                "gen": "matmul.plan",
                "args": {"bt": 3, "m": 16, "n": 64, "k": 64},
            }
        },
        e=expr(X.Load(0)),
        extras=[],
        out=dict(kind="full", pitch=64, rows=48, dtype="f16", offset=0),
    )
    graphs = [
        json.loads(
            rs._rs.generate(
                "matmul.mm_kernel", json.dumps(dict(args, ring_depth=depth)), machine
            )
        )
        for depth in (0, 1, 2)
    ]
    assert graphs[0]["ports"] == graphs[1]["ports"] == graphs[2]["ports"]
    assert all(p["spec"]["space"] == "dram" for p in graphs[0]["scratch"])
    sizes = []
    for graph in graphs[1:]:
        assert all(p["spec"]["space"] == "stage" for p in graph["scratch"])
        sizes.append(sum(port_bytes(p, machine) for p in graph["scratch"]))
        compiled = rs._rs.compile(json.dumps(graph), machine, cache_dir="", fetch=True)
        assert not compiled["verdict"]["refused"]
    assert sizes == [16 * 64 * 2, 2 * 16 * 64 * 2]


def test_identity_read_map_removes_copy_not_just_a_launch():
    dev = Device("offline", board="vlt_v9t6s_1n")
    consumer = row_kernel(dev, X.op("max", X.Load(0), 0))
    spec = consumer.traced.graph["ports"][0]["spec"]
    graph = dict(
        consumer.traced.graph,
        name="full_copy",
        ports=[
            dict(name="x", role="in", spec=spec),
            dict(name="y", role="out", spec=spec),
        ],
        scratch=[],
        body=[
            dict(
                kind="mover",
                op="copy",
                src=dict(buffer="x", axes=["all", "all"]),
                dst=dict(buffer="y", axes=["all", "all"]),
                kw=[],
                flags=[],
            )
        ],
    )
    source = NativeLaunch(graph, {"x": 0x100000, "y": 0x200000}, "move")
    target = NativeLaunch(
        consumer.traced.graph, {"in0": 0x200000, "out": 0x300000}, "dsl"
    )
    off, _ = fuse(
        [source, target],
        [(0x300000, 4096)],
        dev.machine.machine_json,
        quant=False,
        elementwise=False,
        layout=False,
        stage_bytes=1 << 20,
    )
    on, rules = fuse(
        [source, target],
        [(0x300000, 4096)],
        dev.machine.machine_json,
        quant=False,
        elementwise=False,
        layout=True,
        stage_bytes=1 << 20,
    )
    assert len(off) == 2 and len(on) == 1 and rules == ["layout"]
    assert on[0].graph is target.graph and on[0].bindings["in0"] == 0x100000
    assert copy_source(source) == "x"
    changed = dict(
        graph, body=[dict(graph["body"][0], kw=[["walk", {"walk": [[16, 128]]}]])]
    )
    assert copy_source(NativeLaunch(changed, source.bindings, "move")) is None
    dev.close()
