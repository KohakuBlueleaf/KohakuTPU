"""Device-side finite contract, lazy collection and replay on the card model."""

import os

import numpy as np
import pytest

from application.tests.runtime.test_packing_model import reference
from kohakutpu.application.kernels import matmul
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.packages import Refused
from kohakutpu.application.runtime.packing import NonfinitePackingError
from kohakutpu.language.numerics import mxfp7
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


@pytest.fixture
def checked_device():
    if os.environ.get("KOHAKUTPU_DEVICE") != "sim":
        pytest.skip("checked fault cases use a fresh isolated simulator, not silicon")
    dev = D.Device(
        kind="sim",
        board="vlt_v9t6s_1n",
        nodes=1,
        build=os.environ["KOHAKUTPU_SIM_BUILD"],
        classes=False,
    )
    dev.machine = machine_of("vlt_v9t6s_1n", nodes=1, packing_backend="vector_mx7")
    yield dev
    try:
        dev.close()
    except NonfinitePackingError:
        pass


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("pitch", [32, 32768, 65536])
def test_two_slot_backedges_and_slot_alias_mutant(checked_device, side, pitch):
    dev = checked_device
    dev.machine = machine_of(
        "vlt_v9t6s_1n", nodes=1, packing_backend="vector_mx7", packing_looped=True
    )
    data = (
        (np.arange(224 * 32, dtype=np.float32) % 97 / 64 - 0.5)
        .astype("<f2")
        .reshape(224, 32)
    )
    # Independent NUM.MX7 encoding; not unpack(pack(...)).
    quantized, exponent, mantissa = mxfp7.quantise_fp16(data)
    fields = ((exponent + 20) << 3) | (mantissa - 8)
    expected = bytearray()
    for group in range(56):
        scale = sum(
            int(fields[4 * group + lane, 0]) << (24 - 8 * lane) for lane in range(4)
        )
        for word in range(4):
            bits = scale
            for lane in range(4):
                for k in range(8):
                    slot = lane * 8 + k if side == "a" else k * 4 + lane
                    bits |= (int(quantized[4 * group + lane, word * 8 + k]) & 127) << (
                        249 - 7 * slot
                    )
            expected.extend(bits.to_bytes(32, "little"))
    text = f"target ktpu.v9\nfn f.l2(x: f16[224,{pitch}] @dram, q: mx7{side}[224,32] @dram(g{'m' if side == 'a' else 'n'}=56,nk=1))\n    quant q <- x[0 : +224, 0 : +32]\n"
    x, q = dev.alloc(224 * pitch * 2), dev.alloc(data.size)
    try:
        if pitch == 32:
            dev.write(x, data.tobytes())
        else:
            # Sparse view: touch only live bytes plus an adjacent NaN guard.
            for row in range(224):
                dev.write(
                    x + row * pitch * 2,
                    data[row].tobytes() + bytes.fromhex("017c") * 16,
                )
        good = K.compile(text, machine=dev.machine)
        dev.run(good, {"x": x, "q": q})
        assert dev.read(q, data.size) == expected
        if pitch != 32:
            assert "label mx7_batch_" in good.l1_text
            return
        with rs.mutant("l2.packing/alias-slots"):
            bad = K.compile(text, machine=dev.machine)
        # Aliased slots violate fill/unpack ordering; the static gate kills it
        # before submission, rather than allowing a numerical corruption.
        with pytest.raises(Refused, match="L1.VER.009"):
            dev.run(bad, {"x": x, "q": q})
    finally:
        dev.free(x)
        dev.free(q)


@pytest.mark.parametrize("side", ["a", "b"])
def test_absolute_outer_offset_mutant_changes_independent_bytes(checked_device, side):
    dev = checked_device
    text = f"target ktpu.v9\nfn f.l2(x: f16[32,64] @dram, q: mx7{side}[32,64] @dram(g{'m' if side == 'a' else 'n'}=8,nk=1))\n    quant q <- x\n"
    data = (
        (np.arange(2048, dtype=np.float32) % 97 / 64 - 0.5)
        .astype("<f2")
        .reshape(32, 64)
    )
    expected = reference(data, side, 8, 1)
    x, q = dev.alloc(data.nbytes), dev.alloc(data.size)
    try:
        dev.write(x, data.tobytes())
        good = K.compile(text, machine=dev.machine)
        dev.run(good, {"x": x, "q": q})
        assert dev.read(q, data.size) == expected
        with rs.mutant("l2.packing/absolute-zero"):
            bad = K.compile(text, machine=dev.machine)
        dev.run(bad, {"x": x, "q": q})
        assert dev.read(q, data.size) != expected
    finally:
        dev.free(x)
        dev.free(q)


def compiled(dev, side):
    return K.specialise(
        *matmul.quant_kernel(
            Operand("full", pitch=64, rows=8),
            5,
            48,
            8,
            64,
            side,
            2,
            2,
        ),
        machine=dev.machine,
    )


@pytest.mark.parametrize("side", ["a", "b"])
@pytest.mark.parametrize("bits", [0x7C00, 0xFC00, 0x7C01, 0x7DFF, 0x7E00, 0xFFFF])
def test_live_nonfinite_named_lazy_failure_no_host_read(
    checked_device, side, bits, monkeypatch
):
    dev = checked_device
    c = compiled(dev, side)
    data = np.zeros((8, 64), dtype="<u2")
    data[4, 47] = bits
    x, q = dev.alloc(data.nbytes), dev.alloc(512)
    dev.write(x, data.tobytes())
    monkeypatch.setattr(
        dev, "read", lambda *a, **kw: pytest.fail("packing host input read")
    )
    submitted = dev.submit(c, {"x": x, "q": q})
    assert submitted.run is None
    with pytest.raises(NonfinitePackingError, match="NONFINITE_FP16") as caught:
        dev.collect(submitted)
    assert caught.value.completion.status == 0x24
    assert caught.value.completion.detail == 1
    assert submitted.run is None
    with pytest.raises(RuntimeError, match="discarded"):
        dev._healthy()
    dev.free(x)
    dev.free(q)


@pytest.mark.parametrize("side", ["a", "b"])
def test_nonfinite_source_padding_is_overwritten_and_recording_replays(
    checked_device, side
):
    dev = checked_device
    c = compiled(dev, side)
    bits = np.zeros((8, 64), dtype="<u2")
    bits[5:, :] = 0x7C01
    bits[:, 48:] = 0x7C00
    x, q = dev.alloc(bits.nbytes), dev.alloc(512)
    dev.write(x, bits.tobytes())
    dev.record()
    dev.run(c, {"x": x, "q": q})
    log = dev.recorded()
    try:
        assert not any(event[0] == "read" for event in log)
        dev.replay(log)
        # All live values zero: the E=0 field 0x01 in each lane (NUM.MX7.008),
        # zero signed slots.
        assert dev.read(q, 512) == (bytes([0x01]) * 4 + bytes(28)) * 16
        bits[0, 0] = 0xFC00
        dev.write(x, bits.tobytes())
        with pytest.raises(NonfinitePackingError, match="NONFINITE_FP16"):
            dev.replay(log)
        with pytest.raises(RuntimeError, match="discarded"):
            dev.read(q, 512)
    finally:
        log.close()
        dev.free(x)
        dev.free(q)


@pytest.mark.parametrize("side", ["a", "b"])
def test_drop_check_mutant_breaks_refusal_property(checked_device, side):
    dev = checked_device
    with rs.mutant("l2.packing/drop-finite-check"):
        c = compiled(dev, side)
    bits = np.zeros((8, 64), dtype="<u2")
    bits[0, 0] = 0x7C01
    x, q = dev.alloc(bits.nbytes), dev.alloc(512)
    try:
        dev.write(x, bits.tobytes())
        # The exact refusal property above would fail: mutant wrongly succeeds.
        result = dev.run(c, {"x": x, "q": q})
        assert result.cycles > 0
        assert "vcmplt" not in c.l1_text and "assert_zero" in c.l1_text
    finally:
        dev.free(x)
        dev.free(q)
