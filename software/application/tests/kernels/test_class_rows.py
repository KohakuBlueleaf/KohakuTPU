"""Class-aware rows compile witnesses; no execution or value model."""

import json
import re

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


@pytest.mark.parametrize("core_policy", ["all", "min"])
@pytest.mark.parametrize("rows", [1, 16, 17, 32, 48])
@pytest.mark.parametrize("width", [96, 128])
@pytest.mark.parametrize("reduction", [None, "sum", "max", "prod"])
def test_rows_class_compiles(core_policy, rows, width, reduction):
    machine = OfflineCard("multimesh_v9t6").machine
    e = X.op("add", X.Load(0), 1.0)
    if reduction:
        e = X.Reduce(reduction, e)
    output = V.Operand("row") if reduction else V.Operand("full", pitch=width)
    kernel, given, blocks = V.rows_kernel(
        e,
        (V.Operand("full", pitch=width, rows=rows),),
        rows,
        width,
        output,
        machine.count("VC"),
        shape_class=ShapeClass(32, 256, cores=core_policy),
    )
    traced = kernel.trace(given, machine=K.dsl_machine(machine))
    assert "classed=1" in traced.text
    result = rs._rs.compile(
        json.dumps(traced.graph), rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert not result["verdict"]["refused"], result["verdict"]
    assert result["packages"] and not result["bound"], result["bound"]
    assert "skipz" in result["l1"]
    assert blocks == 1
    assert "slack=0" in result["l1"]
    if core_policy == "all":
        assert sum(
            line.startswith("image ") for line in result["l1"].splitlines()
        ) == machine.count("VC")


@pytest.mark.parametrize(
    "mutant", ["l2.class/no-idle", "l2.class/no-mask", "l2.class/share-images"]
)
def test_class_guard_mask_and_image_shape_mutations(mutant):
    machine = OfflineCard("multimesh_v9t6").machine
    kernel, given, _ = V.rows_kernel(
        X.Reduce("sum", X.Load(0)),
        (V.Operand("full", pitch=96),),
        32,
        96,
        V.Operand("row"),
        machine.count("VC"),
        shape_class=ShapeClass(32, 256),
    )
    trace = kernel.trace(given, machine=K.dsl_machine(machine))

    def compile_one(mutation):
        return rs._rs.compile(
            json.dumps(trace.graph),
            rs.machine_json(machine),
            cache_dir="",
            fetch=True,
            mutant=mutation,
        )

    def witness(result):
        text = result["l1"]
        assert not result["verdict"]["refused"]
        assert sum(
            line.startswith("image ") for line in text.splitlines()
        ) == machine.count("VC")
        assert len(re.findall(r"\bskipz s", text)) == 2 * machine.count("VC")
        assert len(re.findall(r"\bvsel ", text)) == 16 * machine.count("VC")

    witness(compile_one(None))
    with pytest.raises(AssertionError):
        witness(compile_one(mutant))


def _class_chain(depth):
    machine = OfflineCard("multimesh_v9t6").machine
    e = X.Load(0)
    for _ in range(depth):
        e = X.op("add", e, 1.0)
    kernel, given, _ = V.rows_kernel(
        e,
        (V.Operand("full", pitch=128),),
        32,
        128,
        V.Operand("full", pitch=128),
        machine.count("VC"),
        shape_class=ShapeClass(32, 256, cores="min"),
    )
    trace = kernel.trace(given, machine=K.dsl_machine(machine))
    return machine, kernel, trace


def test_class_loop_is_serial_when_two_trips_overflow():
    machine, kernel, trace = _class_chain(32)
    args = (json.dumps(trace.graph), rs.machine_json(machine))
    result = rs._rs.compile(*args, cache_dir="", fetch=True)
    assert not result["verdict"]["refused"], result["verdict"]
    text = result["l1"]
    loops = re.findall(r"(?m)^    loop s15\n((?:        .*\n)+)", text)
    assert len(loops) == kernel.class_info["cores"]
    assert text.count("skipz ") == 2 * len(loops)
    for body in loops:
        assert body.count("vadd ") == 32 * 16
        assert body.count("vfill ") == 1
        assert body.rstrip().endswith("vbar")
    unchanged = rs._rs.compile(
        *args, cache_dir="", fetch=True, mutant="l2.vector/no-serial-loop"
    )
    assert unchanged["l1"] == text
    doubled = re.sub(
        r"(?m)^(    loop s15\n)((?:        .*\n)+)",
        lambda m: m[1] + m[2] + m[2],
        text,
    )
    with pytest.raises(rs._rs.Refused, match=r'ISA.VC.043.*VLOOP: field "body"'):
        rs._rs.compile(
            doubled, rs.machine_json(machine), trace.name, l1=True, cache_dir=""
        )


def test_class_single_trip_overflow_is_refused():
    machine, _, trace = _class_chain(64)
    with pytest.raises(rs._rs.Refused, match=r'ISA.VC.043.*VLOOP: field "body"'):
        rs._rs.compile(
            json.dumps(trace.graph), rs.machine_json(machine), cache_dir="", fetch=True
        )
