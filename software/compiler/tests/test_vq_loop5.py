"""Mutation and identity witnesses for immutable zero images and class bands."""

import json
import re

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def zero_compile(reuse, mutant=None):
    source = f"""target ktpu.v9
fn zero.l2(x: f16[6,12544] @dram, q: mx7b[32,12544] @dram(gn=8,nk=2))
    quant q <- x pad=1 finite_batch=4 overlap=1 pipeline=1 zero_classifier=1 stream_axes=1 fill_overlap=1 zero_reuse={reuse}
"""
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    result = rs._rs.compile(
        source, rs.machine_json(machine), cache_dir="", fetch=True, mutant=mutant
    )
    assert not result["verdict"]["refused"], result["verdict"]
    return result


def test_zero_image_hoist_reverse_witness():
    baseline = zero_compile(0)
    result = zero_compile(1)

    def witness(result):
        images = [
            i
            for i in result["l1"].split("\nimage ")
            if "mx7b" in i and "vfill " not in i and "loop s0" in i
        ]
        assert images
        for image in images:
            before, loop = image.split("seti s0 = trips", 1)
            assert "vpack v0" in before
            assert "vpack " not in loop.split("\nfn ")[0]
            assert "vsync fe on=drain" in loop

    witness(result)
    undone = zero_compile(1, "l2.packing/repeated-zero")
    assert undone["l1"] == baseline["l1"]
    with pytest.raises(AssertionError):
        witness(undone)


def class_compile(band, monkeypatch, mutant=None):
    if band is None:
        monkeypatch.delenv("KTPU_CLASS_BAND", raising=False)
    else:
        monkeypatch.setenv("KTPU_CLASS_BAND", str(band))
    kernel, given, _ = V.rows_kernel(
        X.Reduce("sum", X.Load(0)),
        (V.Operand("full", pitch=128),),
        32,
        128,
        V.Operand("row"),
        1,
        shape_class=ShapeClass(32, 256),
    )
    machine = machine_of("vlt_v9t6s_1n")
    graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
    source = json.dumps(graph)
    result = rs._rs.compile(
        source, rs.machine_json(machine), cache_dir="", fetch=True, mutant=mutant
    )
    assert not result["verdict"]["refused"], result["verdict"]
    return (
        result,
        json.loads(rs._rs.library_key(source, rs.machine_json(machine)))["class"],
    )


def test_class_band_independent_registers_and_reverse(monkeypatch):
    baseline, key1 = class_compile(1, monkeypatch)
    result, key4 = class_compile(4, monkeypatch)

    def witness(result):
        registers = set(re.findall(r"vunpk (v\d+)", result["l1"]))
        assert len(registers) > len(set(re.findall(r"vunpk (v\d+)", baseline["l1"])))

    witness(result)
    assert key1 != key4
    default, default_key = class_compile(None, monkeypatch)
    assert default_key == key4
    assert default["l1"] == result["l1"]
    undone, _ = class_compile(4, monkeypatch, "l2.class/serial-band")
    with pytest.raises(AssertionError):
        witness(undone)


def test_native_quant_schedule_identity_and_default():
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    args = dict(
        src=dict(kind="full", pitch=32, rows=0, offset=0, dtype="f16"),
        rows=64,
        cols=32,
        rp=64,
        cp=32,
        side="a",
        g=16,
        nk=1,
    )
    sources = []
    for schedule in (
        None,
        dict(
            stream_axes=0, fill_overlap=0, pipeline=0, zero_classifier=0, zero_reuse=0
        ),
    ):
        request = dict(args)
        if schedule is not None:
            request["schedule"] = schedule
        source = rs._rs.generate(
            "matmul.quant_kernel", json.dumps(request), rs.machine_json(machine), None
        )
        graph = json.loads(source)
        attrs = dict(graph["body"][-1]["kw"])
        assert attrs["stream_axes"]["int"] == int(schedule is None)
        assert attrs["zero_reuse"]["int"] == int(schedule is None)
        compiled = rs._rs.compile(
            source, rs.machine_json(machine), cache_dir="", fetch=True
        )
        assert not compiled["verdict"]["refused"], compiled["verdict"]
        sources.append(
            json.loads(rs._rs.library_key(source, rs.machine_json(machine)))["class"]
        )
    assert sources[0] != sources[1]


def test_quant_zero_default_graph_identity(monkeypatch):
    from_default = []
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    monkeypatch.delenv("KTPU_QUANT_ZERO_REUSE", raising=False)
    for enabled in (True, False):
        if not enabled:
            monkeypatch.setenv("KTPU_QUANT_ZERO_REUSE", "0")
        kernel, given = MM.quant_kernel(
            V.Operand("full", pitch=128), 6, 128, 32, 128, "b", 8, 2
        )
        graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
        nodes = [node for node in graph["body"] if node.get("op") == "quant"]
        assert nodes
        assert any(k == "zero_reuse" for k, _ in nodes[0]["kw"]) == enabled
        from_default.append(
            json.loads(rs._rs.library_key(json.dumps(graph), rs.machine_json(machine)))[
                "class"
            ]
        )
    assert from_default[0] != from_default[1]
