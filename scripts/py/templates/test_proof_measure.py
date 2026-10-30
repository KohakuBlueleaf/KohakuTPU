"""Proof-cost and source replay ledgers must classify real artifacts and expose misses."""

import json

import proof_inventory
import proof_measure
import pytest
import replay_capture
from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.template_store import TemplateStore
from kohakutpu.stack import rs


def fixture(root):
    device = Device(kind="offline", board="multimesh_v9t6")
    machine = rs.machine_json(device.machine)
    device.close()
    source = rs._rs.generate(
        "vector.flat_kernel",
        json.dumps(
            {
                "e": {"nodes": [{"load": 0}], "root": 0},
                "dtypes": ["f16"],
                "n": 128,
                "out_dtype": "f16",
            }
        ),
        machine,
    )
    member = json.loads(rs._rs.library_key(source, machine))["class"]
    capture = {
        "board": "multimesh_v9t6",
        "batch": 1,
        "sources": {member: {"source": source}},
        "launches": [member, member],
    }
    rs._rs.library_prebuild(str(root), source, machine)
    output = rs._rs.library_get(str(root), source, machine)
    package = output["packages"][0]["bytes"]
    table = json.dumps(
        {"parameters": {"unused": {"lo": 0, "hi": 1000000}}, "sites": []}
    )
    buffers = rs._rs.package_read(package, machine)["buffers"]
    bindings = [0x10000000 + i * 0x1000000 for i in range(len(buffers))]
    TemplateStore(root, machine).admit(source, package, table, bindings)
    ClassLibrary(root, machine).admit(
        source,
        output,
        {0: {"source": source, "table": table, "params": {"unused": 12345}}},
        {0: bindings},
    )
    return capture


def test_real_proof_and_admission_cost_ledger(tmp_path):
    capture = fixture(tmp_path)
    result = proof_measure.measure(tmp_path, capture, 1)
    assert result["counts"] == {"PROVEN": 1}
    assert result["templates"] == 1
    assert result["compared_member_variants"] == 1
    assert result["proven_launch_candidates"] == 2
    assert result["member_byte_proof_seconds"] > 0
    assert result["capability_bind_seconds"] > 0
    result = proof_measure.measure(tmp_path, capture, 0)
    assert result["counts"] == {"REFUSED": 1}
    assert result["proven_launch_candidates"] == 0
    with pytest.raises(RuntimeError, match="missing current-machine"):
        proof_measure.measure(tmp_path / "missing", capture, 1)


def test_default_proof_budget_and_inventory(tmp_path, monkeypatch):
    monkeypatch.delenv("KOHAKUTPU_TEMPLATE_PROOF_CAP", raising=False)
    assert TemplateStore(tmp_path, "{}").proof_cap == 2048
    monkeypatch.setenv("KOHAKUTPU_TEMPLATE_PROOF_CAP", "1")
    assert TemplateStore(tmp_path, "{}").proof_cap == 1
    capture = fixture(tmp_path)
    rows = proof_inventory.inventory(tmp_path, capture)
    assert len(rows) == 1
    row = next(iter(rows.values()))
    assert row["exhaustive_product"] == 1
    assert row["dimensions"]["unused"]["data_only"] is True
    assert row["dimensions"]["unused"]["sites"] == []


@pytest.mark.parametrize("classes", [False, True])
def test_capture_replay_exposes_compiler_bypass(tmp_path, monkeypatch, classes):
    capture = fixture(tmp_path)
    result = replay_capture.replay(tmp_path, capture, classes)
    assert result["library_hits"] == 2
    assert result["class_hits"] == (2 if classes else 0)
    assert result["compile_calls"] == result["library_misses"] == 0

    def bypass(*args, **kwargs):
        return rs._rs.compile("", "")

    monkeypatch.setattr(replay_capture.Device, "kernel", bypass)
    with pytest.raises(AssertionError, match="compiler"):
        replay_capture.replay(tmp_path, capture, classes)
