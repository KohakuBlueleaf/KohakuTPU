"""Reverse witnesses for opt-in stream batching and direct compact classes."""

import json
from types import SimpleNamespace

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime import costs
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def quant(knob, enabled, mutant=None):
    source = f"""target ktpu.v9
fn test.l2(x: f16[64,32] @dram, q: mx7a[64,32] @dram(gm=16,nk=1))
    quant q <- x finite_batch=4 pipeline=1 zero_classifier=1 stream_axes=1 fill_overlap=1 {knob}={enabled}
"""
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    got = rs._rs.compile(
        source, rs.machine_json(machine), cache_dir="", fetch=True, mutant=mutant
    )
    assert not got["verdict"]["refused"], got["verdict"]
    return got["l1"]


@pytest.mark.parametrize(
    "knob,mutant",
    [
        ("stream_batch", "l2.packing/short-batches"),
        ("classifier_banks", "l2.packing/single-classifier-bank"),
    ],
)
def test_quant_reverse(knob, mutant):
    before = quant(knob, 0)
    after = quant(knob, 1)
    assert after != before
    assert quant(knob, 1, mutant) == before


def test_direct_class_admission_identity(monkeypatch):
    machine = machine_of("vlt_v9t6s_1n")
    variants = []
    for value in (0, 1):
        monkeypatch.setenv("KTPU_CLASS_ROWS_DIRECT", str(value))
        kernel, given, _ = V.rows_kernel(
            X.Reduce("sum", X.Load(0)),
            (V.Operand("full", pitch=128),),
            32,
            128,
            V.Operand("row"),
            1,
            shape_class=ShapeClass(32, 256),
        )
        graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
        assert kernel.class_info["direct"] == bool(value)
        assert any(n.get("op") == "copy" for n in graph["body"]) != bool(value)
        variants.append(
            json.loads(rs._rs.library_key(json.dumps(graph), rs.machine_json(machine)))[
                "class"
            ]
        )
    assert variants[0] != variants[1]
    # Partial rows/columns cannot inherit full-trip direct accesses.
    for rows, width in ((33, 128), (32, 96)):
        kernel, _, _ = V.rows_kernel(
            X.Load(0),
            (V.Operand("full", pitch=128),),
            rows,
            width,
            V.Operand("full", pitch=128),
            1,
            shape_class=ShapeClass(32, 256),
        )
        assert not kernel.class_info["direct"]


def test_route_shape_and_alias_admission(monkeypatch):
    dev = SimpleNamespace(machine=machine_of("vlt_v9t6s_1n"))
    monkeypatch.setenv("KTPU_CLASS_ROWS_DIRECT", "1")
    monkeypatch.setenv("KTPU_CLASS_ROWS_ROUTE", "1")
    monkeypatch.setattr(RW.FM, "launch", lambda *a, **kw: "class")
    monkeypatch.setattr(RW.FM.generated, "launch", lambda *a, **kw: "exact")
    args = (dev, X.Reduce("sum", X.Load(0)), [V.Operand("full", pitch=128)])
    tail = (32, 128, V.Operand("row"), {}, 1)
    assert RW._launch(*args, dict(in0=0x10000, out=0x20000), *tail)[0] == "class"
    assert RW._launch(*args, dict(in0=0x10000, out=0x10000), *tail)[0] == "exact"
    monkeypatch.setenv("KTPU_CLASS_ROWS_ROUTE", "0")
    assert RW._launch(*args, dict(in0=0x10000, out=0x20000), *tail)[0] == "exact"


def test_route_capacity_falls_back(monkeypatch):
    dev = SimpleNamespace(machine=machine_of("vlt_v9t6s_1n"))
    monkeypatch.setenv("KTPU_CLASS_ROWS_DIRECT", "1")
    monkeypatch.setenv("KTPU_CLASS_ROWS_ROUTE", "1")
    monkeypatch.setattr(RW.FM.generated, "launch", lambda *a, **kw: "exact")
    operands = [
        V.Operand("full", pitch=256),
        V.Operand("full", pitch=256),
        V.Operand("row"),
    ]
    expression = X.op("add", X.op("add", X.Load(0), X.Load(1)), X.Load(2))
    assert (
        RW._launch(
            dev,
            expression,
            operands,
            dict(in0=0x10000, in1=0x20000, in2=0x30000, out=0x40000),
            16,
            256,
            V.Operand("full", pitch=256),
            {},
            1,
        )[0]
        == "exact"
    )
    # The class really is infeasible: a forced admission kills this witness.
    with pytest.raises(ValueError, match="do not fit class L1"):
        V.rows_kernel(
            expression,
            tuple(operands),
            16,
            256,
            V.Operand("full", pitch=256),
            1,
            shape_class=ShapeClass(32, 256),
        )


def test_direct_profile_provenance(tmp_path, monkeypatch):
    for name in (
        "KTPU_QUANT_CLASSIFIER_OVERLAP",
        "KTPU_QUANT_PIPELINE",
        "KTPU_QUANT_ZERO_CLASSIFIER",
        "KTPU_QUANT_STREAM_AXES",
        "KTPU_QUANT_FILL_OVERLAP",
        "KTPU_QUANT_ZERO_REUSE",
        "KTPU_PULL_TREE",
        "KTPU_CLASS_ROWS_DIRECT",
        "KTPU_CLASS_ROWS_ROUTE",
    ):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("KTPU_CLASS_BAND", "1")
    monkeypatch.setenv("KTPU_PIPE_FINITE_BATCH", "4")
    monkeypatch.setenv("KTPU_PIPE_OVERLAP", "1")
    path = tmp_path / "costs.json"
    path.write_text(
        json.dumps(
            dict(
                schedule=dict(finite_batch=4, overlap=True),
                recipes={
                    costs.key("staged_padding", dict(board="test", recipe={})): dict(
                        original=10, alternative=1, verified=True
                    )
                },
            )
        )
    )
    monkeypatch.setenv("KTPU_COST_PROFILE", str(path))
    assert costs.choose("staged_padding", {}, "test")
    for name in ("KTPU_CLASS_ROWS_DIRECT", "KTPU_CLASS_ROWS_ROUTE"):
        monkeypatch.setenv(name, "1")
        assert not costs.choose("staged_padding", {}, "test")
        monkeypatch.setenv(name, "0")
