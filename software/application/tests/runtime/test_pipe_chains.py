"""Different prepared consumer rules: compound walk and matmul extra."""

import json
import os
from pathlib import Path

import numpy as np
from application.tests.runtime.test_pipe_batch import model  # noqa: F401

from kohakutpu.stack import kernel as K


def test_prepared_compound_and_matmul_extra_bytes(model):  # noqa: F811
    dev = model
    root = Path(os.environ["KTPU_PIPE_CHAIN_ARTIFACTS"])
    for index in (2632, 2458):
        rec = json.loads((root / f"chains/{index}.json").read_text())
        buffers = {p: dev.alloc(n + 256) for p, n in rec["allocations"].items()}
        data = {
            p: (
                bytes.fromhex(rec["tables"][p])
                if p in rec["tables"]
                else ((np.arange(n // 2) % 73 - 36) / 32).astype(np.float16).tobytes()
            )
            for p, n in rec["allocations"].items()
        }
        outputs = []
        for stages in (rec["before"], [rec["after"]]):
            for p, raw in data.items():
                dev.write(buffers[p], bytes([0xD3]) * 128 + raw + bytes([0xD3]) * 128)
            for stage in stages:
                c = K.compile(
                    stage["l1"], name=stage["name"], machine=dev.machine, level="l1"
                )
                assert not c.verdict["refused"]
                dev.run(c, {p: buffers[b] + 128 for p, b in stage["binds"].items()})
            p = rec["output"]
            out = dev.read(buffers[p], rec["allocations"][p] + 256)
            assert out[:128] == out[-128:] == bytes([0xD3]) * 128
            outputs.append(out)
        assert outputs[0] == outputs[1]
        for addr in buffers.values():
            dev.free(addr)
