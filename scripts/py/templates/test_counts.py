"""Count-path cost witnesses use wire packages, not a device or value model."""

import json

import counts
import pytest

from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF


@pytest.mark.parametrize("broken", [False, True])
def test_active_step_site_cost_and_missing_prefix_mutant(tmp_path, monkeypatch, broken):
    machine_json = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    package = _rs.package_build(
        json.dumps({"credit": 512}),
        json.dumps([["unit", [0, 0], "VC"], ["dispatch", 0, [0]], ["payload", 0]]),
        machine_json,
    )
    table = {
        "parameters": {"n": {"lo": 1, "hi": 2}},
        "sites": [
            {
                "name": "count",
                "step": 0,
                "bit": 32,
                "width": 32,
                "coefficients": {"n": 1},
            }
        ],
    }
    capture = {
        "records": [{"generator": "fixture", "arguments": {"n": 2}}],
        "launches": [{"record": 0}],
        "machine_json": (MF.MACHINES / "multimesh_v9t6.json").read_text(
            encoding="utf-8"
        ),
    }
    ledger = {
        "artifacts": [{"launches": [0], "meshes": {"0": "fixture"}}],
        "generators": {"fixture": {"proven": 1}},
    }
    root = tmp_path / "experimental-templates"
    root.mkdir()
    (root / "fixture.bin").write_bytes(package)
    (root / "fixture.json").write_text(json.dumps(table), encoding="utf-8")
    cap, result, output = (
        tmp_path / name for name in ("capture.json", "results.json", "costs.json")
    )
    cap.write_text(json.dumps(capture), encoding="utf-8")
    result.write_text(json.dumps(ledger), encoding="utf-8")
    if broken:
        original = counts._rs.bind

        def no_prefix(*args, **kwargs):
            return original(*args, **dict(kwargs, count_path="step"))

        monkeypatch.setattr(counts._rs, "bind", no_prefix)
        with pytest.raises(AssertionError, match="count-path cost differs"):
            counts.measure(cap, result, output)
    else:
        counts.measure(cap, result, output)
        measured = json.loads(output.read_text(encoding="utf-8"))
        assert measured["totals"] == {
            "extra_steps": 2,
            "extra_payloads": 0,
            "extra_bytes": 32,
            "step_sites": 1,
        }
