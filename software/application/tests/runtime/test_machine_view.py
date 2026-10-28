"""The runtime view retains schema facts and rejects absent meshes."""

import json
from pathlib import Path

import pytest

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("name", MF.names())
def test_runtime_view_matches_frozen_runtime(name):
    cases = json.loads(
        (ROOT / "software/rust/golden/bindings/card.json").read_text(encoding="utf-8")
    )["cases"]
    for case in cases:
        if case["machine"] != name:
            continue
        inp, want = case["input"], case["output"]
        spec = machine_of(name, inp["nodes"], inp["units"])
        assert spec.board == name
        assert spec.params == want["view"]["params"]
        assert spec.stage_bytes == want["view"]["stage_bytes"]
        for mesh, offset, global_addr, stage_addr in want["addresses"]:
            assert spec.global_addr(offset, mesh) == global_addr
            assert spec.stage_addr(offset, mesh) == stage_addr
        assert rs.machine_json(spec) == spec.machine_json
        with pytest.raises(ValueError):
            spec.stage_addr(0, len(spec.meshes))


def test_runtime_schema_is_not_replaced_by_legacy_profile_defaults():
    spec = machine_of("multimesh_v9t6", nodes=1)
    doc = json.loads(rs.machine_json(spec))
    assert doc["machine"]["vc"]["regs"] == 128
    assert doc["machine"]["vc"]["rf_copies"] == 2
    assert doc["machine"]["card"]["nodes"] == 1
    with pytest.raises(ValueError):
        spec.global_addr(0, 1)
