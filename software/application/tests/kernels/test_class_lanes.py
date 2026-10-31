"""Fixed lanes class structure and independent index-table padding witnesses."""

import json
from types import SimpleNamespace

import numpy as np
import pytest

from kohakutpu.application.runtime import generated as G
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import rs

MACHINE = OfflineCard("multimesh_v9t6").machine
DEV = SimpleNamespace(machine=MACHINE)


def class_source(taps, words, keep):
    arguments = {
        "class": {"taps": taps, "words": words, "keep": keep},
        "src_words": 4096,
        "dst_words": 4096,
        "core_policy": "all",
    }
    return G.source(DEV, "lanes.class_kernel", arguments)


def compile_class(taps, words, keep):
    result = rs._rs.compile(
        class_source(taps, words, keep),
        rs.machine_json(MACHINE),
        fetch=True,
        cache_dir="",
    )
    assert result["bound"] is None
    assert not result["verdict"]["refused"], result["verdict"]
    return result


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize("words", [1, 7, 8, 65, 192])
def test_class_compiles_fixed_crossbars(words, keep):
    result = compile_class([[0, 0x5555], [1, 0xAAAA]], words, keep)
    assert "loop s15" in result["l1"]
    assert "skipz s15" in result["l1"]


@pytest.mark.parametrize("taps", [1, 9])
def test_parameter_rotations_keep_package_structure(taps):
    sources = []
    for last in (0, 1, 7, 15):
        rotations = [[last if i == taps - 1 else 0, 1 << i] for i in range(taps)]
        graph = json.loads(class_source(rotations, 64, False))
        sources.append(json.dumps(dict(graph, name="template")))
    outputs = rs._rs.template_compile(
        sources[0], sources, rs.machine_json(MACHINE), parameter_rotations=True
    )
    packages = [
        rs._rs.package_read(out["packages"][0]["bytes"], rs.machine_json(MACHINE))
        for out in outputs
    ]
    assert all(not out["verdict"]["refused"] for out in outputs)
    assert all(package["steps"] == packages[0]["steps"] for package in packages)
    assert all(
        len(package["payloads"]) == len(packages[0]["payloads"]) for package in packages
    )
    assert outputs[0]["packages"][0]["bytes"] != outputs[1]["packages"][0]["bytes"]


def test_padding_duplicates_a_complete_member():
    made = G.relayout_plan(
        "lanes.plan", 64, ((3, 17), (7, 1)), 0, ((3, 9), (7, 1)), 0, 32, True
    )
    plan = G.planned(DEV, made)
    at = 0
    for c in G.lanes_tables(DEV, made)["classes"]:
        cls, index = c["class"], c["index"]
        taps = len(cls["taps"])
        rows = index.reshape(taps + 1, 8, -1)
        entries = -(-rows.shape[-1] // 8) * 8
        original = plan["index"][at : at + (taps + 1) * 8 * entries].reshape(
            taps + 1, 8, -1
        )
        for k in range(rows.shape[-1] * 8):
            source = min(k, cls["words"] - 1)
            np.testing.assert_array_equal(
                rows[:, k % 8, k // 8], original[:, source % 8, source // 8]
            )
        assert c["masks"].shape == (taps, 128)
        at += original.size
