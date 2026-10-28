"""The host buffer facade keeps every frozen packed byte and decoded value."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF
from kohakutpu.stack.buffer import Buffer

GOLDEN = Path(__file__).resolve().parents[2] / "rust/golden/bindings/buffer.json"
CASES = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]


def same(raw, want):
    if isinstance(want, str):
        return raw.hex() == want
    return len(raw) == want["len"] and hashlib.sha256(raw).hexdigest() == want["sha256"]


@pytest.mark.parametrize("case", CASES, ids=[c["case"] for c in CASES])
def test_buffer_facade(case):
    text = (MF.MACHINES / f"{case['machine']}.json").read_text(encoding="utf-8")
    want = case["output"]
    if "refused" in want["buffer"]:
        with pytest.raises(_rs.Refused):
            Buffer.of(case["input"]["type"], text)
        return
    b = Buffer.of(case["input"]["type"], text)
    assert b.nbytes == want["buffer"]["bytes"]
    assert b.space == want["buffer"]["space"]
    assert b.layout["node"] == want["buffer"]["node"]
    rng = np.random.default_rng(case["input"]["seed"])
    if b.dtype.startswith("mx7") or b.dtype in ("f16", "f32"):
        host = rng.standard_normal(b.host_shape).astype(b.host_dtype)
    else:
        host = rng.integers(0, 8, b.host_shape).astype(b.host_dtype)
    packed = b.pack(host)
    assert same(packed, want["pack"])
    if isinstance(want["unpack"], dict) and "refused" in want["unpack"]:
        with pytest.raises(_rs.Refused):
            b.unpack(packed)
    else:
        assert same(b.unpack(packed).tobytes(), want["unpack"])
