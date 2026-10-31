"""Flat classes preserve scalar lane semantics and cover element-count tails."""

import json
from types import SimpleNamespace

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.classes import values
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime import generated as G
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import rs


@pytest.mark.parametrize("dtype", ["f16", "f32"])
@pytest.mark.parametrize("lane", [0, 7, 15])
def test_flat_class_tail_bind_and_scalar_lane(dtype, lane):
    machine = OfflineCard("multimesh_v9t6").machine
    machine_json = rs.machine_json(machine)
    policy = ShapeClass(32, 256)

    def compile_n(n):
        source, info = G.classed(
            SimpleNamespace(machine=machine),
            "vector.flat_kernel",
            {
                "e": G.expr(X.op("add", X.Load(0), X.Load(1))),
                "dtypes": [dtype, "f16"],
                "n": n,
                "out_dtype": dtype,
                "scalars": [[1, lane]],
            },
            policy,
        )
        graph = json.loads(source)
        out = next(p for p in graph["ports"] if p["name"] == "out")
        assert out["spec"]["shape"] == [-(-n // 128), 128]
        result = rs._rs.compile(
            json.dumps(dict(graph, name="template")),
            machine_json,
            cache_dir="",
            fetch=True,
        )
        assert not result["verdict"]["refused"]
        assert f"lane={lane}" in result["l1"]
        return result["packages"][0], info

    bounds = {"n": {"lo": 1, "hi": 2049}}
    points = json.loads(rs._rs.template_points(json.dumps(bounds)))
    samples = [compile_n(p["n"]) for p in points]
    recipes = json.dumps(values.rows(samples[0][1]))
    template = rs._rs.template_derive(
        [p["bytes"] for p, _ in samples],
        json.dumps(points),
        json.dumps(bounds),
        machine_json,
        values_json=recipes,
    )
    for n in [
        1,
        2,
        127,
        128,
        129,
        255,
        256,
        257,
        511,
        512,
        513,
        1023,
        1024,
        1025,
        2047,
        2048,
        2049,
    ]:
        package, _ = compile_n(n)
        params = rs._rs.template_values(recipes, json.dumps({"n": n}))
        bindings = [0x10000000 + i * 0x100000 for i in range(len(package["params"]))]
        rs._rs.template_prove(
            template["bytes"],
            template["template_json"],
            package["bytes"],
            params,
            bindings,
            machine_json,
            f"n={n}",
        )
