"""Partial/empty entries across class batches and nk chunks, no DRAM padding."""

import json
import os
from pathlib import Path

import numpy as np

from application.tests.runtime.test_packing_model import reference
from application.tests.runtime.test_pipe_batch import model  # noqa: F401
from kohakutpu.stack import kernel as K


def test_class_batched_padding_bytes_and_mutant(model):  # noqa: F811
    dev = model
    recs = json.loads(Path(os.environ["KTPU_PIPE_GENERAL_ARTIFACT"]).read_text())
    # Source guards are signaling NaNs; valid rows include signed zeros and
    # independent finite magnitudes. No padded word may be read or classified.
    data = np.full((11, 80), 0x7C01, np.uint16)
    valid = (np.arange(2 * 5 * 48).reshape(2, 5, 48) % 79 / 32 - 1).astype(np.float16)
    valid[:, 0, 0] = -0.0
    for b in range(2):
        data[b * 5 : b * 5 + 5, 16:64] = valid[b].view(np.uint16)
    padded = np.zeros((32, 128), np.float16)
    for b in range(2):
        padded[b * 16 : b * 16 + 5, :48] = valid[b]
    want = reference(padded, "b", 2, 2)
    x, q = dev.alloc(data.nbytes), dev.alloc(padded.size + 256)
    dev.write(x, data.tobytes())
    for label in ("good", "walk", "l2.packing/pad-nonzero"):
        rec = recs[label]
        c = K.compile(rec["l1"], name=rec["name"], machine=dev.machine, level="l1")
        assert not c.verdict["refused"]
        assert "fill w(" not in c.l1_text
        if label != "walk":
            assert "copy w(" not in c.l1_text
        dev.write(q, bytes([0xD3]) * (padded.size + 256))
        dev.run(c, {"x": x, "q": q + 128})
        got = dev.read(q, padded.size + 256)
        assert got[:128] == got[-128:] == bytes([0xD3]) * 128
        assert (got[128:-128] == want) == (label != "l2.packing/pad-nonzero"), label
    dev.free(x)
    dev.free(q)
