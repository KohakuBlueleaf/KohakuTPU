"""Bounded byte parity and cycle evidence for finite MX7 batch scheduling."""

import json
import os

import numpy as np
import pytest

from application.tests.runtime.test_packing_model import reference
from kohakutpu.application.kernels import matmul
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K


@pytest.mark.parametrize("checked", [False, True])
def test_quant_batch_padding_slots_and_bytes(monkeypatch, checked):
    if os.environ.get("KOHAKUTPU_DEVICE") != "sim":
        pytest.skip("requires the isolated v9t6s simulator")
    # Both L1 slots, a nonzero source offset and a partial three-row tile.
    data = np.full((132, 48), np.nan, dtype="<f2")
    live = ((np.arange(131 * 16) % 97 - 48) / 32).astype("<f2").reshape(131, 16)
    data[:131, 16:32] = live
    padded = np.zeros((132, 32), dtype="<f2")
    padded[:131, :16] = live
    expected = reference(padded, "b", 33, 1)
    records = []
    choices = ((4, 0), (4, 1)) if checked else ((1, 0), (4, 0))
    for batch, overlap in choices:
        dev = D.Device(
            kind="sim",
            board="vlt_v9t6s_1n",
            nodes=1,
            build=os.environ["KOHAKUTPU_SIM_BUILD"],
            classes=False,
        )
        dev.machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
        x, q = dev.alloc(data.nbytes), dev.alloc(len(expected) + 256)
        try:
            dev.write(x, data.tobytes())
            monkeypatch.setenv("KTPU_QUANT_CLASSIFIER_OVERLAP", str(overlap))
            monkeypatch.setenv("KTPU_PIPE_FINITE_BATCH", str(batch))
            monkeypatch.setenv("KTPU_PIPE_OVERLAP", "1")
            kernel, given = matmul.quant_kernel(
                Operand("full", pitch=48, offset=16),
                131,
                16,
                132,
                32,
                "b",
                33,
                1,
                finite=not checked,
            )
            compiled = K.specialise(kernel, given, machine=dev.machine)
            dev.write(q, bytes([0xD3]) * (len(expected) + 256))
            run = dev.run(compiled, {"x": x, "q": q + 128}, kind="quant")
            actual = dev.read(q, len(expected) + 256)
            assert actual[:128] == actual[-128:] == bytes([0xD3]) * 128
            assert actual[128:-128] == expected
            assert dev.read(x, data.nbytes) == data.tobytes()
            records.append(
                dict(
                    finite_batch=batch,
                    checked=checked,
                    classifier_overlap=overlap,
                    cycles=run.cycles,
                    read_bytes=live.nbytes,
                    write_bytes=len(expected),
                )
            )
        finally:
            dev.free(x)
            dev.free(q)
            counters = dev.close()
        records[-1]["counters"] = counters
    print(json.dumps(records), flush=True)
