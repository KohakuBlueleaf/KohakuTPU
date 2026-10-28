"""Fixed lanes class structure and independent index-table padding witnesses."""

import json

import numpy as np
import pytest

from kohakutpu.application.kernels import lanes as L
from kohakutpu.application.kernels.classes import lanes as C
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def compile_class(cls):
    machine = OfflineCard("multimesh_v9t6").machine
    kernel, given = C.kernel(cls, 4096, 4096, 2)
    source = json.dumps(kernel.trace(given, machine=K.dsl_machine(machine)).graph)
    result = rs._rs.compile(source, rs.machine_json(machine), fetch=True, cache_dir="")
    assert result["bound"] is None
    assert not result["verdict"]["refused"], result["verdict"]
    return result


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("words", [1, 7, 8, 65, 192])
def test_class_compiles_fixed_crossbars(words, keep):
    cls = L.Class(((0, 0x5555), (1, 0xAAAA)), words, keep)
    result = compile_class(cls)
    assert "loop s15" in result["l1"]
    assert "skipz s15" in result["l1"]


@pytest.mark.parametrize("taps", [1, 9])
def test_parameter_rotations_keep_package_structure(taps):
    machine = OfflineCard("multimesh_v9t6").machine
    sources = []
    for last in (0, 1, 7, 15):
        cls = L.Class(
            tuple((last if i == taps - 1 else 0, 1 << i) for i in range(taps)),
            64,
            False,
        )
        kernel, given = C.kernel(cls, 4096, 4096, 2)
        graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
        sources.append(json.dumps(dict(graph, name="template")))
    outputs = rs._rs.template_compile(
        sources[0], sources, rs.machine_json(machine), parameter_rotations=True
    )
    packages = [
        rs._rs.package_read(out["packages"][0]["bytes"], rs.machine_json(machine))
        for out in outputs
    ]
    assert all(not out["verdict"]["refused"] for out in outputs)
    assert all(package["steps"] == packages[0]["steps"] for package in packages)
    assert all(
        len(package["payloads"]) == len(packages[0]["payloads"]) for package in packages
    )
    assert outputs[0]["packages"][0]["bytes"] != outputs[1]["packages"][0]["bytes"]


def test_padding_duplicates_a_complete_member():
    plan = L.plan(64, ((3, 17), (7, 1)), 0, ((3, 9), (7, 1)), 0, 32, True)
    at = 0
    for cls, index, masks in C.tables(plan):
        rows = index.reshape(len(cls.taps) + 1, 8, -1)
        original = plan.index[
            at : at + (len(cls.taps) + 1) * 8 * L.entries(cls)
        ].reshape(len(cls.taps) + 1, 8, -1)
        for k in range(rows.shape[-1] * 8):
            source = min(k, cls.words - 1)
            np.testing.assert_array_equal(
                rows[:, k % 8, k // 8], original[:, source % 8, source // 8]
            )
        assert masks.shape == (len(cls.taps), 128)
        at += original.size
