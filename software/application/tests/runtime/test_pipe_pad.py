"""Native in-place word-tail padding, byte identity and poisoned padding witness."""

import json
import os
from pathlib import Path

import numpy as np
import pytest
from application.tests.runtime.test_packing_model import reference
from application.tests.runtime.test_pipe_batch import model  # noqa: F401

from kohakutpu.stack import kernel as K


def records():
    path = os.environ.get("KTPU_PIPE_PAD_ARTIFACTS")
    if not path:
        pytest.skip("native host-lowered padding artifacts required")
    return json.loads(Path(path).read_text())


def test_padding_bytes(model, tmp_path):  # noqa: F811
    dev = model
    side = "b"
    all_records = records()
    rng = np.random.default_rng(402)
    bits = rng.integers(0, 65536, (224, 48), dtype=np.uint16)
    bits[(bits & 0x7C00) == 0x7C00] = 0x7BFF
    bits[0] = 0x8000
    bits[1, :16] = np.arange(16, dtype=np.uint16)
    data = bits.view(np.float16)
    expected = reference(np.pad(data, ((0, 0), (0, 16))), side, 56, 1)
    x, q = dev.alloc(data.nbytes + 128), dev.alloc(224 * 64 + 256)
    dev.write(x, data.tobytes() + bytes.fromhex("017c") * 64)
    metrics = []
    for pad, batch in ((0, 4), (1, 4)):
        rec = all_records[f"224-{side}-{pad}-{batch}-good"]
        c = K.compile(rec["l1"], name=rec["name"], machine=dev.machine, level="l1")
        assert not c.verdict["refused"]
        for _ in range(2):
            dev.write(q, bytes([0xD3]) * (224 * 64 + 256))
            run = dev.run(c, {"x": x, "q": q + 128})
            raw = dev.read(q, 224 * 64 + 256)
            assert raw[128:-128] == expected
            assert raw[:128] == raw[-128:] == bytes([0xD3]) * 128
        metrics.append(dict(pad=pad, batch=batch, cycles=run.cycles))
    rec = all_records[f"224-{side}-1-4-l2.packing/pad-nonzero"]
    bad = K.compile(rec["l1"], name=rec["name"], machine=dev.machine, level="l1")
    dev.run(bad, {"x": x, "q": q + 128})
    assert dev.read(q + 128, 224 * 64) != expected
    # The metrics go to KTPU_PIPE_PAD_OUT when set.
    path = (
        Path(os.environ.get("KTPU_PIPE_PAD_OUT") or tmp_path) / f"pad-model-{side}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, indent=2))
    dev.free(x)
    dev.free(q)
