"""Unresolved whole-card proofs carry their reason through runtime refusal."""

import numpy as np
import pytest

from kohakutpu.application.runtime import forms
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.packages import Finding, Refused
from kohakutpu.stack.kernel import compile


def test_unknown_memory_proof_reports_the_refusing_reason():
    reason = "global DRAM proof budget exhausted"
    with pytest.raises(Refused, match=reason):
        raise Refused([Finding("model", "shared physical memory", reason)])


def test_index_proof_uses_current_owned_table_bytes(monkeypatch):
    dev = Device(kind="offline", board="multimesh_v9t8", nodes=1)
    try:
        index = forms.table(dev, np.zeros(8, dtype=np.float32), np.float32)
        source = dev.alloc(64)
        output = dev.alloc(64)
        kernel = compile(
            """target ktpu.v9
fn indexed.l1(x: u8[64] @dram, y: u8[64] @dram, i: f32[8] @dram)
    move
        gather x -> w(y, (1, 32)) index=i n=1 pitch=32 words=1 fp
""",
            machine=dev.machine,
            level="l1",
        )
        binds = {"x": source, "y": output, "i": index}
        dev.run(kernel, binds)
        dev.write(index, np.full(8, np.nan, dtype=np.float32).tobytes())
        with pytest.raises(Refused, match="invalid floating mover index"):
            dev.run(kernel, binds)

        def failed_write(*args):
            raise OSError("table upload interrupted")

        valid = np.zeros(8, dtype=np.float32).tobytes()
        with monkeypatch.context() as patch:
            patch.setattr(dev.card, "write", failed_write)
            with pytest.raises(OSError, match="table upload interrupted"):
                dev.write(index, valid)
        with pytest.raises(Refused, match="indexed mover footprint"):
            dev.run(kernel, binds)
        dev.write(index, valid)
        dev.run(kernel, binds)
    finally:
        dev.close()
