"""Wide class schedules keep independent maps and distinguish template keys."""

import json

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def compile_wide(monkeypatch, width, direct, rows=16):
    monkeypatch.setenv("KTPU_CLASS_WIDE_WIDTH", str(width))
    monkeypatch.setenv("KTPU_CLASS_WIDE_DIRECT", str(direct))
    kernel, given, _ = V.rows_kernel(
        X.op("max", X.Load(0), 0.0),
        (V.Operand("full", pitch=512),),
        rows,
        512,
        V.Operand("full", pitch=512),
        1,
        shape_class=ShapeClass(32, 256),
    )
    machine = machine_of("vlt_v9t6s_1n")
    source = json.dumps(kernel.trace(given, machine=K.dsl_machine(machine)).graph)
    compiled = rs._rs.compile(
        source, rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert not compiled["verdict"]["refused"], compiled["verdict"]
    key = json.loads(rs._rs.library_key(source, rs.machine_json(machine)))
    return kernel.class_info, compiled, key


def test_wide_width_reverse_witness(monkeypatch):
    narrow = compile_wide(monkeypatch, 32, 0)
    wide = compile_wide(monkeypatch, 256, 0)

    def witness(result):
        assert result[0]["width"] == 256
        assert "desc a0=" in result[1]["l1"]
        assert "(32, 64)" in result[1]["l1"]

    witness(wide)
    assert narrow[2]["class"] != wide[2]["class"]
    with pytest.raises(AssertionError):
        witness(narrow)  # reversing the width transformation must kill the witness


def test_wide_direct_reverse_witness_and_tail_admission(monkeypatch):
    staged = compile_wide(monkeypatch, 256, 0)
    direct = compile_wide(monkeypatch, 256, 1)

    def witness(result):
        assert result[0]["lowering"] == "direct-blocks"
        assert "copy w(" not in result[1]["l1"]
        assert "fill w(" not in result[1]["l1"]
        assert "vunpk " in result[1]["l1"]

    witness(direct)
    assert staged[2]["class"] != direct[2]["class"]
    with pytest.raises(AssertionError):
        witness(staged)
    tail_off = compile_wide(monkeypatch, 256, 0, rows=17)
    tail_on = compile_wide(monkeypatch, 256, 1, rows=17)
    assert tail_off[1]["l1"] == tail_on[1]["l1"]
    assert tail_off[2]["class"] == tail_on[2]["class"]
