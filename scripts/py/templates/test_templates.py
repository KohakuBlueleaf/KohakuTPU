"""Offline extension and capture witnesses; no simulator or card is opened."""

import copy
import json

import pytest
from capture import encode
from derive import decode, parameters, with_parameters
from kohakutpu.application.kernels import convert, expr, vector
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def test_expression_dag_identity_and_mutant():
    shared = expr.op("add", expr.Load(0), expr.Load(1))
    e = expr.op("mul", shared, shared)
    got = decode(json.loads(json.dumps(encode(e))))
    assert got.args[0] is got.args[1]
    mutant = expr.Op(got.name, (got.args[0], copy.deepcopy(got.args[1])))
    assert mutant.args[0] is not mutant.args[1]


def test_copy_word_parameter():
    assert parameters({"nbytes": 33}) == {"nbytes:words32": 2}
    assert with_parameters({"nbytes": 33}, {"nbytes:words32": 3}) == {"nbytes": 96}


def test_copy_affine_derivation_independent_launch_and_dropped_term():
    machine = OfflineCard("multimesh_v9t6").machine
    text = rs.machine_json(machine)
    bounds = {"words": {"lo": 1, "hi": 9}}
    points = json.loads(rs._rs.template_points(json.dumps(bounds)))

    def direct(words):
        kernel, given = convert.copy_kernel(words * 32)
        graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
        graph["name"] = "template"
        out = rs._rs.compile(
            json.dumps(graph), text, name="template", cache_dir="", fetch=True
        )
        assert not out["verdict"]["refused"]
        return out["packages"][0]["bytes"]

    packages = [direct(p["words"]) for p in points]
    template = rs._rs.template_derive(
        packages, json.dumps(points), json.dumps(bounds), text
    )
    launch = direct(6)
    params = json.dumps({"words": 6})
    bindings = [0x10000000, 0x20000000]
    rs._rs.template_prove(
        template["bytes"],
        template["template_json"],
        launch,
        params,
        bindings,
        text,
        "independent launch",
    )
    table = json.loads(template["template_json"])
    site = next(s for s in table["sites"] if s["coefficients"].get("words"))
    del site["coefficients"]["words"]
    with pytest.raises(ValueError, match="first difference"):
        rs._rs.template_prove(
            template["bytes"],
            json.dumps(table),
            launch,
            params,
            bindings,
            text,
            "dropped-term mutant",
        )


def test_pinned_compile_rejects_new_search_choice():
    machine = OfflineCard("multimesh_v9t6").machine
    text = rs.machine_json(machine)

    def source(e):
        kernel, given = vector.flat_kernel(
            e, ("f16",), 2048, "f16", machine.count("VC")
        )
        graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
        return json.dumps(dict(graph, name="template"))

    representative = source(expr.Load(0))
    repeated = rs._rs.template_compile(representative, [representative], text)
    direct = rs._rs.compile(representative, text, cache_dir="", fetch=True)
    assert repeated[0]["packages"][0]["bytes"] == direct["packages"][0]["bytes"]
    with pytest.raises(ValueError, match="L2.TEMPLATE.CHOICE"):
        rs._rs.template_compile(
            representative, [source(expr.op("neg", expr.Load(0)))], text
        )


def test_missing_corner_is_not_a_template():
    with pytest.raises(ValueError, match="empty sample set"):
        machine = OfflineCard("multimesh_v9t6").machine
        rs._rs.template_derive(
            [], "[]", '{"n":{"lo":1,"hi":2}}', rs.machine_json(machine)
        )
