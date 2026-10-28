"""Numeric upload and lane precision at the Rust buffer/machine seam."""

import json

import numpy as np

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel, rs
from kohakutpu.stack.buffer import Buffer


def test_fp16_numeric_upload_saturates_but_raw_bits_are_not_reencoded():
    b = Buffer.of("f16[4] @dram", rs.machine_json("multimesh_v9t6"))
    assert b.pack([70000.0, -1e6, 65520.0, 65504.0]).hex() == "ff7bfffbff7bff7b"
    raw = np.array([0x7C00, 0x7E00, 1, 0x8000], "<u2").view(np.float16)
    assert b.pack(raw).hex() == "007c007e01000080"


def test_lane_precision_reaches_the_tracer_and_rust_overlay(monkeypatch, tmp_path):
    m = machine_of("multimesh_v9t6")
    assert kernel.dsl_machine(m).sig_bits == 16
    doc = json.loads(m.machine_json)
    doc["machine"]["vc"]["sig_bits"] = 18
    (tmp_path / "wide.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(MF, "MACHINES", tmp_path)
    wide = machine_of("wide")
    assert kernel.dsl_machine(wide).sig_bits == 18
    assert json.loads(rs.machine_json(wide))["machine"]["vc"]["sig_bits"] == 18
