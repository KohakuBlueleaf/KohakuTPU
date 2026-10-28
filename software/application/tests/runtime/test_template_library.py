"""Exact-class library hits bind handles without invoking the compiler."""

import json

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.template_store import TemplateStore
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def graph(dev, n=128):
    kernel, given = V.flat_kernel(X.op("add", X.Load(0), 1.0), ("f16",), n, "f16", 2)
    return json.dumps(kernel.trace(given, machine=K.dsl_machine(dev.machine)).graph)


def test_hit_names_and_handles_without_compilation(tmp_path, monkeypatch):
    dev = Device(
        kind="offline", board="multimesh_v9t6", library=tmp_path, library_only=True
    )
    source = graph(dev)
    machine = rs.machine_json(dev.machine)
    result = rs._rs.library_prebuild(str(tmp_path), source, machine)
    renamed = json.loads(source)
    renamed["name"] = "another_diagnostic_name"
    assert rs._rs.library_key(source, machine) == rs._rs.library_key(
        json.dumps(renamed), machine
    )
    assert not result["hit"]
    artifact = rs._rs.library_get(str(tmp_path), source, machine)
    packages = {
        mesh: package["bytes"] for mesh, package in artifact["packages"].items()
    }
    tables = dict.fromkeys(packages, json.dumps({"parameters": {}, "sites": []}))
    bindings = {
        mesh: [
            0x10000000 + i * 0x1000000
            for i in range(len(rs._rs.package_read(raw, machine, mesh)["buffers"]))
        ]
        for mesh, raw in packages.items()
    }
    assert json.loads(
        rs._rs.template_verify(packages, tables, bindings, machine, "exact")
    ) == [{}]
    image_tables = {
        mesh: json.dumps({"parameters": {}, "sites": [], "images": []})
        for mesh in packages
    }
    for mesh, raw in packages.items():
        assert (
            json.loads(rs._rs.template_certificate(raw, image_tables[mesh], machine))[
                "data_only"
            ]
            == {}
        )
    assert json.loads(
        rs._rs.template_verify(
            packages, image_tables, bindings, machine, "exact", confined=True
        )
    ) == [{}]
    with pytest.raises(ValueError, match="budget"):
        rs._rs.template_verify(packages, tables, bindings, machine, "exact", cap=0)

    def forbidden(*args, **kwargs):
        raise AssertionError("compiler invoked on library hit")

    monkeypatch.setattr(rs._rs, "compile", forbidden)
    output = dev.compile_graph(json.dumps(renamed))
    compiled = generated.from_output(dev, renamed, output)
    x, y = dev.alloc(256), dev.alloc(256)
    dev.run(compiled, {"in0": x, "out": y})
    assert dev.counters["library_hits"] == 1
    assert dev.counters["compile_calls"] == 0
    assert dev.counters["runs"] == 1
    dev.free(x)
    dev.free(y)
    dev.close()


def test_class_store_requires_certificate_and_enforces_domain(tmp_path, monkeypatch):
    dev = Device(kind="offline", board="multimesh_v9t6")
    source = graph(dev)
    machine = rs.machine_json(dev.machine)
    rs._rs.library_prebuild(str(tmp_path), source, machine)
    artifact = rs._rs.library_get(str(tmp_path), source, machine)
    package = artifact["packages"][0]["bytes"]
    table = json.dumps({"parameters": {"unused": {"lo": 1, "hi": 2}}, "sites": []})
    buffers = rs._rs.package_read(package, machine)["buffers"]
    bindings = [0x10000000 + i * 0x1000000 for i in range(len(buffers))]
    store = TemplateStore(tmp_path, machine)
    identifier = store.admit(source, package, table, bindings)
    translations = [(base, base, b["size"]) for base, b in zip(bindings, buffers)]
    original_bind = rs._rs.bind
    assert store.bind_handles(source, table, {"unused": 1}, bindings, translations)
    with pytest.raises(ValueError, match="outside"):
        store.bind_handles(source, table, {"unused": 3}, bindings, translations)

    def bypass(raw, handles, machine_json, **kwargs):
        return original_bind(
            raw, handles, machine_json, mutant="l0.template/class", **kwargs
        )

    monkeypatch.setattr(rs._rs, "bind", bypass)
    assert store.bind_handles(source, table, {"unused": 3}, bindings, translations)
    key = store.identity(source, table)
    path = tmp_path / key["machine"] / "classes" / f"{identifier}.json"
    entry = json.loads(path.read_text(encoding="utf-8"))
    del entry["certificate"]
    path.write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        store.load(source, table)
    dev.close()


@pytest.mark.parametrize("verification", ["proof", "both"])
def test_whole_box_capability_admits_unseen_point_without_member_proof(
    tmp_path, monkeypatch, verification
):
    dev = Device(kind="offline", board="multimesh_v9t6")
    source = graph(dev)
    machine = rs.machine_json(dev.machine)
    rs._rs.library_prebuild(str(tmp_path), source, machine)
    package = rs._rs.library_get(str(tmp_path), source, machine)["packages"][0]["bytes"]
    table = json.dumps(
        {"parameters": {"unused": {"lo": 1, "hi": 1000000}}, "sites": []}
    )
    buffers = rs._rs.package_read(package, machine)["buffers"]
    bindings = [0x10000000 + i * 0x1000000 for i in range(len(buffers))]
    store = TemplateStore(tmp_path, machine, verification=verification)
    identifier = store.admit(source, package, table, bindings)
    proof = store.proven(identifier, bindings, cap=1)
    entry = json.loads(
        next(
            p for p in tmp_path.glob("*/classes/*.json") if p.stem == identifier
        ).read_text(encoding="utf-8")
    )
    assert entry["proof"]["status"] == "PROVEN"
    assert entry["coverage_kind"] == ("proof" if verification == "proof" else "sample")
    with pytest.raises(TypeError):
        rs._rs.TemplateProof()
    with pytest.raises(AttributeError):
        proof.certificate = "forged"
    with pytest.raises(ValueError, match="verification"):
        TemplateStore(tmp_path, machine, verification="invalid")
    certificate = json.loads(proof.certificate)
    assert certificate["checker_bindings"] == 1
    assert certificate["eliminated"] == ["unused"]

    def forbidden(*args, **kwargs):
        raise AssertionError("compiler or per-member byte proof invoked")

    monkeypatch.setattr(rs._rs, "compile", forbidden)
    monkeypatch.setattr(rs._rs, "template_prove", forbidden)
    params = {"unused": 765432}
    assert proof.bind(json.dumps(params)) == rs._rs.bind(
        package, bindings, machine, params=params, template_json=table
    )
    with pytest.raises(ValueError, match="outside"):
        proof.bind(json.dumps({"unused": 1000001}))
    verdict = json.loads(
        rs._rs.template_verify(
            {0: package},
            {0: table},
            {0: bindings},
            machine,
            "proof",
            cap=0,
            mode="proof",
        )
    )
    assert verdict["status"] == "REFUSED"
    with pytest.raises(ValueError, match="mode"):
        rs._rs.template_verify(
            {0: package}, {0: table}, {0: bindings}, machine, "bad", mode="bad"
        )
    dev.close()


def test_miss_and_stale_stage_key_refuse(tmp_path):
    dev = Device(
        kind="offline", board="multimesh_v9t6", library=tmp_path, library_only=True
    )
    source = graph(dev)
    machine = rs.machine_json(dev.machine)
    key = json.loads(rs._rs.library_key(source, machine))
    with pytest.raises(RuntimeError, match=key["class"]):
        dev.compile_graph(source)
    rs._rs.library_prebuild(str(tmp_path), source, machine)
    path = tmp_path / key["machine"] / (key["class"] + ".json")
    artifact = json.loads(path.read_text(encoding="utf-8"))
    artifact["key"]["versions"]["lower"] = "stale"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(RuntimeError, match=key["class"]):
        dev.compile_graph(source)
    assert dev.counters["library_misses"] == 2
    assert dev.counters["compile_calls"] == 0
    dev.close()


def test_bypass_mutant_is_counted_and_refused(tmp_path, monkeypatch):
    dev = Device(
        kind="offline", board="multimesh_v9t6", library=tmp_path, library_only=True
    )
    source = graph(dev)

    def bypass(source):
        with rs.compiler_owner(dev):
            return rs.compile(source, json.loads(source)["name"], dev.machine)

    monkeypatch.setattr(dev, "compile_graph", bypass)
    with pytest.raises(RuntimeError, match="forbids compiler call"):
        dev.compile_graph(source)
    assert dev.counters["compile_calls"] == 1
    dev.close()


def test_scalar_expression_preserves_lane_broadcast():
    dev = Device(kind="offline", board="multimesh_v9t6")
    args = {
        "e": {
            "nodes": [
                {"load": 0},
                {"const": 1.0},
                {"op": "add", "args": [0, 1]},
                {"load": 1},
                {"op": "mul", "args": [2, 3]},
            ],
            "root": 4,
        },
        "dtypes": ["f16", "f16"],
        "n": 10,
        "scalars": [[0, 0]],
    }
    machine = rs.machine_json(dev.machine)
    source = rs._rs.generate("vector.flat_kernel", json.dumps(args), machine)
    assert not rs._rs.compile(source, machine, fetch=True, cache_dir="")["verdict"][
        "refused"
    ]
    with pytest.raises(ValueError, match="store of.*values into"):
        rs._rs.generate(
            "vector.flat_kernel", json.dumps(args), machine, "dsl.ops/lane-lost"
        )
    dev.close()
