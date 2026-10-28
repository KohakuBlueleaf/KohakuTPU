"""Persistent and multi-environment capture admission through production prebuild."""

import json

import prebuild
import pytest

from kohakutpu.application.kernels import convert as KC
from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.device import Device
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


@pytest.mark.parametrize("same_capture", [False, True])
def test_prebuild_environment_variants(tmp_path, same_capture):
    dev = Device(
        kind="offline", board="multimesh_v9t6", library=tmp_path, library_only=True
    )
    try:
        machine = rs.machine_json(dev.machine)
        doc = {"sources": {}, "launches": []}
        results = {}
        for words in (2, 6):
            kernel, given = KC.copy_kernel(words * 32)
            source = kernel.trace(given, machine=K.dsl_machine(dev.machine)).graph_json
            key = json.loads(rs._rs.library_key(source, machine))["class"]
            results[key] = rs._rs.library_prebuild(str(tmp_path), source, machine)
            doc["sources"][key] = {"source": source, "family": "dsl", "aliases": [[]]}
            doc["launches"].append(key)
        if same_capture:
            for entry in doc["sources"].values():
                entry["aliases"].append([["dst", "src", 0]])
        prebuild.prebuild_classes(tmp_path, doc, machine, results, {})
        if not same_capture:
            for entry in doc["sources"].values():
                entry["aliases"] = [[["dst", "src", 0]]]
            prebuild.prebuild_classes(tmp_path, doc, machine, results, {})
        clib = ClassLibrary(tmp_path, machine)
        for entry in doc["sources"].values():
            source = entry["source"]
            variants = [clib.lookup(source), clib.lookup(source, variant=1)]
            assert all(v is not None for v in variants)
            tables = [json.loads(v["packages"][0]["template_json"]) for v in variants]
            assert sorted(t["environment"] for t in tables) == [[], [[0, 1, 0]]]
            size = max(t["buffer_sizes"][0] for t in tables)
            a, b = dev.alloc(size), dev.alloc(size)
            for dst in (a, b, a, b):
                dev.run(dev.kernel(source), {"src": a, "dst": dst})
        assert dev.counters["class_hits"] == 8
        assert dev.counters["exact_hits"] == 0
        assert dev.counters["compile_calls"] == 0
    finally:
        dev.close()
