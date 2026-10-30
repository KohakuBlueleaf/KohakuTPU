"""Different prepared consumer rules: compound walk and matmul extra."""

import numpy as np

from application.tests.runtime.pipe_artifacts import (  # noqa: F401
    chain_record,
    epilogue_record,
    pipe_library,
)
from application.tests.runtime.test_pipe_batch import model  # noqa: F401
from kohakutpu.stack import kernel as K


def output(dev, rec, stages):
    """The record's output bytes after `stages`, None when the verifier
    refuses a stage for a too-small row stride (sub-line ownership)."""
    buffers = {p: dev.alloc(n + 256) for p, n in rec["allocations"].items()}
    for p, n in rec["allocations"].items():
        raw = (
            bytes.fromhex(rec["tables"][p])
            if p in rec["tables"]
            else ((np.arange(n // 2) % 73 - 36) / 32).astype(np.float16).tobytes()
        )
        dev.write(buffers[p], bytes([0xD3]) * 128 + raw + bytes([0xD3]) * 128)
    out = None
    for stage in stages:
        c = K.compile(stage["l1"], name=stage["name"], machine=dev.machine, level="l1")
        if c.verdict["refused"]:
            assert any(f["clause"] == "L1.VER.017" for f in c.verdict["findings"])
            break
        dev.run(c, {p: buffers[b] + 128 for p, b in stage["binds"].items()})
    else:
        p = rec["output"]
        out = dev.read(buffers[p], rec["allocations"][p] + 256)
        assert out[:128] == out[-128:] == bytes([0xD3]) * 128
    for addr in buffers.values():
        dev.free(addr)
    return out


def wrong_pitch(rec):
    after = rec["after"]
    assert "pitch=64" in after["l1"]
    return [dict(after, l1=after["l1"].replace("pitch=64", "pitch=32"))]


def test_prepared_compound_bytes(model, chain_record):  # noqa: F811
    rec = chain_record
    assert output(model, rec, rec["before"]) == output(model, rec, [rec["after"]])


def test_prepared_compound_wrong_pitch(model, chain_record):  # noqa: F811
    rec = chain_record
    assert output(model, rec, [rec["after"]]) != output(model, rec, wrong_pitch(rec))


def test_prepared_matmul_extra_bytes_and_mutant(model, epilogue_record):  # noqa: F811
    rec = epilogue_record
    want = output(model, rec, rec["before"])
    assert want is not None and want == output(model, rec, [rec["after"]])
    l1 = rec["after"]["l1"]
    assert "vadd " in l1
    wrong = dict(rec["after"], l1=l1.replace("vadd ", "vmul "))
    assert want != output(model, rec, [wrong])
