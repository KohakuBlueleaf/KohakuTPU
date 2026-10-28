"""Byte-equality witnesses across a rows class, including all interior tails."""

import json

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes import values
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def test_class_walk_has_fixed_payload_count_under_singleton_mutant():
    machine = OfflineCard("multimesh_v9t6").machine

    def build(rows, mutant):
        kernel, given, _ = V.rows_kernel(
            X.Load(0),
            (V.Operand("full", pitch=96),),
            rows,
            96,
            V.Operand("full", pitch=96),
            machine.count("VC"),
            shape_class=ShapeClass(32, 256),
        )
        graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
        result = rs._rs.compile(
            json.dumps(dict(graph, name="template")),
            rs.machine_json(machine),
            cache_dir="",
            fetch=True,
            mutant=mutant,
        )
        assert not result["verdict"]["refused"]
        return rs._rs.package_read(
            result["packages"][0]["bytes"], rs.machine_json(machine)
        )

    def witness(mutant):
        a, b = build(1, mutant), build(2, mutant)
        assert a["steps"] == b["steps"]
        assert len(a["payloads"]) == len(b["payloads"])

    witness(None)
    with pytest.raises(AssertionError):
        witness("l2.class/squeeze-walk")


@pytest.mark.parametrize("stat", [False, True])
def test_one_template_all_row_counts(stat):
    machine = OfflineCard("multimesh_v9t6").machine
    machine_json = rs.machine_json(machine)
    shape_class = ShapeClass(32, 256)

    def compile_rows(rows):
        expression = X.op("add", X.Load(0), 1.0)
        output = V.Operand("row") if stat else V.Operand("full", pitch=96)
        if stat:
            expression = X.Reduce("sum", expression)
        kernel, given, _ = V.rows_kernel(
            expression,
            (V.Operand("full", pitch=96),),
            rows,
            96,
            output,
            machine.count("VC"),
            shape_class=shape_class,
        )
        graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
        result = rs._rs.compile(
            json.dumps(dict(graph, name="template")),
            machine_json,
            cache_dir="",
            fetch=True,
        )
        assert not result["verdict"]["refused"]
        return result["packages"][0], kernel.class_info

    bounds = {"R": {"lo": 1, "hi": 65}, "W": {"lo": 96, "hi": 96}}
    points = json.loads(rs._rs.template_points(json.dumps(bounds)))
    samples = [compile_rows(p["R"]) for p in points]
    recipes = json.dumps(values.rows(samples[0][1]))
    template = rs._rs.template_derive(
        [p["bytes"] for p, _ in samples],
        json.dumps(points),
        json.dumps(bounds),
        machine_json,
        values_json=recipes,
    )
    for rows in range(1, 66):
        package, _ = compile_rows(rows)
        params = rs._rs.template_values(recipes, json.dumps({"R": rows, "W": 96}))
        bindings = [0x10000000 + i * 0x100000 for i in range(len(package["params"]))]
        rs._rs.template_prove(
            template["bytes"],
            template["template_json"],
            package["bytes"],
            params,
            bindings,
            machine_json,
            f"rows={rows}",
        )
    with pytest.raises(ValueError, match="missing verification point"):
        rs._rs.template_derive(
            [p["bytes"] for p, _ in samples[1:]],
            json.dumps(points[1:]),
            json.dumps(bounds),
            machine_json,
            values_json=recipes,
        )
