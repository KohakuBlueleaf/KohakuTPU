"""Native graph composition and real Rust template admission witnesses."""

import json
from unittest.mock import patch

from examples.graph.native_measure import pilot
from examples.graph.native_reuse import witness

from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.graph.native import compose, fuse, local_scratch
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import rs


def test_native_quant_and_epilogue_remove_external_edge():
    dev = Device("offline", board="vlt_v9t6s_1n")
    dev.machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    launches, output, _, _, _, _ = pilot(dev)
    for quant, elementwise, rule in (
        (True, False, "quant"),
        (False, True, "elementwise"),
    ):
        fused, rules = fuse(
            launches,
            [output],
            dev.machine.machine_json,
            quant=quant,
            elementwise=elementwise,
            stage_bytes=1 << 20,
        )
        assert rules == [rule] and len(fused) == 2
        candidate = next(k for k in fused if k.graph["name"] == "composed_neighbours")
        graph = candidate.graph
        assert any(s["spec"]["space"] == "stage" for s in graph["scratch"])
        compiled = rs._rs.compile(
            json.dumps(graph), dev.machine.machine_json, cache_dir="", fetch=True
        )
        assert not compiled["verdict"]["refused"] and compiled["bound"] is None
        assert any(a["space"] == "stage" for a in compiled["scratch"])
    recursive, rules = fuse(
        launches,
        [output],
        dev.machine.machine_json,
        stage_bytes=1 << 20,
        recursive=True,
    )
    assert len(recursive) == 1 and rules == ["quant", "elementwise"]
    placed = local_scratch(recursive[0], dev.machine.machine_json, 1 << 20)
    assert all(p["spec"]["space"] == "stage" for p in placed.graph["scratch"])
    assert local_scratch(recursive[0], dev.machine.machine_json, 1) is recursive[0]
    dev.close()


def test_native_export_prevents_intermediate_elision():
    dev = Device("offline", board="vlt_v9t6s_1n")
    dev.machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    launches, output, _, _, _, _ = pilot(dev)
    exported = (launches[0].bindings["q"] + 32, 32)
    fused, rules = fuse(
        launches,
        [output, exported],
        dev.machine.machine_json,
        quant=True,
        elementwise=False,
        stage_bytes=1 << 20,
    )
    assert len(fused) == 3 and rules == []
    mutant, rules = fuse(
        launches,
        [output],
        dev.machine.machine_json,
        quant=True,
        elementwise=False,
        stage_bytes=1 << 20,
    )
    assert len(mutant) == 2 and rules == ["quant"]
    dev.close()


def test_native_reduction_is_not_an_elementwise_consumer():
    dev = Device("offline", board="vlt_v9t6s_1n")
    launches, output, _, _, _, _ = pilot(dev)
    right = launches[-1]
    with patch.object(
        right, "graph", dict(right.graph, body=[{"kind": "value", "op": "rsum"}])
    ):
        planned, rules = fuse(
            launches,
            [output],
            dev.machine.machine_json,
            quant=False,
            stage_bytes=1 << 20,
        )
        assert len(planned) == 3 and not rules
    dev.close()


def test_native_composed_class_two_shapes_two_addresses(tmp_path):
    result = witness(tmp_path)
    assert result["certified_templates"] == 1
    assert result["compile_calls"] == 0 and result["class_hits"] == 2
    assert result["runs"] == 4


def test_native_renaming_keeps_both_member_bodies():
    graph = {
        "format": "ktpu.graph",
        "version": 1,
        "name": "x",
        "target": "ktpu.v9",
        "machine": {},
        "ports": [
            {"name": "in0", "role": "in", "spec": {}},
            {"name": "out", "role": "out", "spec": {}},
        ],
        "scratch": [],
        "body": [
            {"kind": "value", "target": "v", "args": [{"view": {"buffer": "in0"}}]},
            {"kind": "store", "src": "v", "dst": {"buffer": "out"}},
        ],
    }
    # NativeLaunch is supplied by the same source adapter used in the pilot.
    dev = Device("offline", board="vlt_v9t6s_1n")
    launches, _, _, _, _, _ = pilot(dev)
    left, right = launches[0], launches[1]
    with patch.object(left, "graph", graph), patch.object(right, "graph", graph):
        merged = compose(left, right, "out", ("in0",))
        assert merged.graph["body"][2]["args"][0]["view"]["buffer"] == "p_out"
        assert merged.graph["body"][3]["src"] == "c_v"
    dev.close()
