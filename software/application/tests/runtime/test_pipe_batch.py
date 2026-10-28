"""Native finite-classifier batching: independent bytes, guards, and wait mutant."""

import json
import os
from pathlib import Path

import numpy as np
import pytest
from application.tests.runtime.test_packing_model import reference

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.packing import NonfinitePackingError
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def compiled(machine, side, batch, mutant=None):
    artifacts = os.environ.get("KTPU_PIPE_ARTIFACTS")
    if artifacts:
        rec = json.loads(Path(artifacts).read_text())[
            f"{side}-{batch}-{mutant or 'good'}"
        ]
        return K.compile(rec["l1"], name="q", machine=machine, level="l1")
    text = (
        "target ktpu.v9\n"
        f"fn q.l2(x: f16[224,32] @dram, q: mx7{side}[224,32] "
        f"@dram(g{'m' if side == 'a' else 'n'}=56,nk=1))\n"
        f"    quant q <- x finite_batch={batch}\n"
    )
    with rs.mutant(mutant):
        return K.compile(text, name="q", machine=machine)


def test_native_batch_verifier_and_missing_wait():
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    good = compiled(machine, "a", 4)
    assert not good.verdict["refused"], good.verdict
    bad = compiled(machine, "a", 4, "l2.packing/batch-no-pack-wait")
    assert bad.verdict["refused"]
    assert any(f["clause"] == "L1.VER.009" for f in bad.verdict["findings"])


@pytest.fixture
def model():
    if os.environ.get("KOHAKUTPU_DEVICE") != "sim":
        pytest.skip("desktop shared-model witness only")
    dev = D.Device(
        kind="sim",
        board="vlt_v9t6s_1n",
        nodes=1,
        build=os.environ["KOHAKUTPU_SIM_BUILD"],
        classes=False,
    )
    dev.machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    yield dev
    try:
        dev.close()
    except NonfinitePackingError:
        pass


def test_native_batch_bytes_and_guard(model):
    dev = model
    side = "a"
    bits = np.random.default_rng(20).integers(0, 65536, (224, 32), dtype=np.uint16)
    bits[(bits & 0x7C00) == 0x7C00] = 0x7BFF
    bits[0] = 0
    bits[1] = 0x8000
    bits[2, :16] = np.arange(16, dtype=np.uint16)
    data = bits.view(np.float16)
    expected = reference(data, side, 56, 1)
    x, q = dev.alloc(data.nbytes), dev.alloc(data.size + 256)
    dev.write(x, data.tobytes())
    records = []
    for batch in (1, 4):
        c = compiled(dev.machine, side, batch)
        dev.write(q, bytes([0xD3]) * (data.size + 256))
        for _ in range(2):
            run = dev.run(c, {"x": x, "q": q + 128})
            raw = dev.read(q, data.size + 256)
            assert raw[128:-128] == expected
            assert raw[:128] == raw[-128:] == bytes([0xD3]) * 128
        records.append((batch, run.cycles))
    assert dev.read(x, data.nbytes) == data.tobytes()
    print("PIPE_BATCH", side, records)
    dev.free(x)
    dev.free(q)


def test_native_batch_nonfinite(model):
    dev = model
    data = np.zeros((224, 32), dtype=np.uint16)
    data[-1, -1] = 0x7C01
    x, q = dev.alloc(data.nbytes), dev.alloc(data.size)
    dev.write(x, data.tobytes())
    c = compiled(dev.machine, "a", 4)
    with pytest.raises(NonfinitePackingError):
        dev.run(c, {"x": x, "q": q})


def test_missing_pack_wait_is_wrong_on_model(model):
    """Only this isolated mutant bypasses the verified static rejection."""
    dev = model
    data = (
        (np.arange(224 * 32, dtype=np.float32) % 97 / 64 - 0.5)
        .astype(np.float16)
        .reshape(224, 32)
    )
    x, q = dev.alloc(data.nbytes), dev.alloc(data.size)
    dev.write(x, data.tobytes())
    bad = compiled(dev.machine, "a", 4, "l2.packing/batch-no-pack-wait")
    assert bad.verdict["refused"]
    bad.verdict = {"refused": False, "findings": []}
    dev.run(bad, {"x": x, "q": q})
    assert dev.read(q, data.size) != reference(data, "a", 56, 1)
    dev.free(x)
    dev.free(q)
