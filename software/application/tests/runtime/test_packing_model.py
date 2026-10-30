"""Direct byte and converter witnesses on the existing read-only card model."""

import json
import os
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest

from kohakutpu.application.kernels import matmul
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms, generated
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.storage import FLAT, Storage
from kohakutpu.language.numerics import mxfp7
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def reference(x, side, groups, nk):
    """NUM.MX7.002/.003 oracle plus independent NUM.MX7.005 slot placement.
    Callers pass the operand as quantised, `mxfp7.lift(x, nk)`."""
    q, es, m8 = mxfp7.quantise_fp16(x)
    rows, cols = x.shape
    fields = ((es + 20) << 3) | (m8 - 8)
    out = bytearray()
    for tile in range(rows // (4 * groups)):
        for chunk in range(cols // (32 * nk)):
            for group in range(groups):
                for block in range(nk):
                    r = tile * 4 * groups + group * 4
                    k = chunk * 32 * nk + block * 32
                    scales = sum(
                        int(fields[r + lane, k // 32]) << (24 - 8 * lane)
                        for lane in range(4)
                    )
                    for word in range(4):
                        bits = scales
                        for lane in range(4):
                            for kk in range(8):
                                slot = lane * 8 + kk if side == "a" else kk * 4 + lane
                                bits |= (int(q[r + lane, k + word * 8 + kk]) & 127) << (
                                    249 - 7 * slot
                                )
                        out.extend(bits.to_bytes(32, "little"))
    return bytes(out)


def program(rows, cols, side, groups, nk, backend):
    return (
        "target ktpu.v9\n\n"
        f"fn pack.l2(x: f16[{rows}, {cols}] @dram, q: mx7{side}[{rows}, {cols}] "
        f"@dram(g{'m' if side == 'a' else 'n'}={groups}, nk={nk}))\n"
        f"    quant q <- x finite=1 backend={backend}\n"
    )


def trial(dev, x, side, groups, nk, backend, mutant=None):
    rows, cols = x.shape
    text = program(rows, cols, side, groups, nk, backend)
    xa, qa = dev.alloc(x.nbytes), dev.alloc(rows * cols + 256)
    try:
        dev.write(xa, x.tobytes())
        dev.write(qa, bytes([0xD3]) * (rows * cols + 256))
        with rs.mutant(mutant):
            c = K.compile(text, machine=dev.machine)
        cold = dev.run(c, {"x": xa, "q": qa + 128})
        result = dev.run(c, {"x": xa, "q": qa + 128})
        data = dev.read(qa, rows * cols + 256)
        assert data[:128] == data[-128:] == bytes([0xD3]) * 128
        assert dev.read(xa, x.nbytes) == x.tobytes()
        metrics = {
            "backend": backend,
            "rows": rows,
            "cols": cols,
            "side": side,
            "cold_cycles": cold.cycles,
            "cycles": result.cycles,
            "cycles_per_packed_byte": result.cycles / (rows * cols),
            "package_bytes": sum(len(p["bytes"]) for p in c.packages.values()),
        }
        print(json.dumps(metrics), flush=True)
        return data[128:-128], metrics
    finally:
        dev.free(xa)
        dev.free(qa)


@pytest.mark.parametrize("side", ["a", "b"])
def test_small_exact_bytes_and_guards(dev, side, tmp_path):
    rng = np.random.default_rng(17)
    bits = rng.integers(0, 65536, (8, 64), dtype=np.uint16)
    bits[(bits & 0x7C00) == 0x7C00] = 0x7BFF
    bits[0] = 0
    bits[1] = 0x8000
    bits[2, :16] = np.arange(16, dtype=np.uint16)
    x = bits.view(np.float16)
    records = []
    # mover QUANT only into unpaired destinations (RUL.099)
    for backend, nk in (("mover_quant", 1), ("vector_mx7", 1), ("vector_mx7", 2)):
        actual, metrics = trial(dev, x, side, 2, nk, backend)
        assert actual == reference(mxfp7.lift(x, nk), side, 2, nk)
        records.append(metrics)
    # The metrics go to KTPU_PACKING_MODEL_OUT when set.
    path = (
        Path(os.environ.get("KTPU_PACKING_MODEL_OUT") or tmp_path)
        / f"model-small-{side}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


@pytest.mark.parametrize(
    "mutant",
    ["l2.packing/wrong-order", "l2.packing/wrong-layout", "l2.packing/wrong-scale"],
)
def test_packed_byte_oracle_kills_mutants(dev, mutant):
    x = (np.arange(512, dtype=np.float16).reshape(8, 64) - 128) / 64
    actual, _ = trial(dev, x, "a", 2, 2, "vector_mx7", mutant)
    assert actual != reference(mxfp7.lift(x, 2), "a", 2, 2)


def test_exhaustive_finite_converter_identity(dev):
    bits = np.arange(65536, dtype="<u2")
    bits = bits[(bits & 0x7C00) != 0x7C00]
    n = bits.size
    text = (
        "target ktpu.v9\n\nimage convert(x, y)\n"
        "    desc a0 = x walk=(32, 8)\n    desc a1 = y walk=(32, 8)\n"
        "    ainc a0 += 256\n    ainc a1 += 256\n"
        f"    seti s0 = {n // 128}\n    loop s0\n"
        "        vfill a0 -> l1[0] rel\n        vsync fe on=fill\n"
        "        vunpk v0 <- l1[0] f16\n        vsync fe on=unpack\n"
        "        vpack v0 -> l1[128] f16\n        vsync fe on=pack\n"
        "        vdrain a1 <- l1[128] rel\n        vsync fe on=drain\n    halt\n\n"
        f"fn convert.l1(x: f16[{n}] @dram, y: f16[{n}] @dram)\n"
        "    send vc[0]\n        run convert(x, y)\n    barrier\n"
    )
    xa, ya = dev.alloc(bits.nbytes), dev.alloc(bits.nbytes)
    try:
        dev.write(xa, bits.tobytes())
        c = K.compile(text, machine=dev.machine, level="l1")
        dev.run(c, {"x": xa, "y": ya})
        assert dev.read(ya, bits.nbytes) == bits.tobytes()
    finally:
        dev.free(xa)
        dev.free(ya)


@pytest.mark.parametrize(
    "shape,groups,nk", [((64, 128), 8, 2), ((136, 192), 17, 3), ((68, 32), 17, 1)]
)
def test_tiles_chunks_and_batch_tails(dev, shape, groups, nk):
    rng = np.random.default_rng(43)
    bits = rng.integers(0, 65536, shape, dtype=np.uint16)
    bits[(bits & 0x7C00) == 0x7C00] = 0x7BFF
    x = bits.view(np.float16)
    for side in ("a", "b"):
        actual, _ = trial(dev, x, side, groups, nk, "vector_mx7")
        assert actual == reference(mxfp7.lift(x, nk), side, groups, nk)


def test_runtime_unaligned_packed_form(dev, monkeypatch):
    monkeypatch.setattr(
        dev, "machine", machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    )
    raw = np.arange(100, dtype=np.float16) / 32
    st = Storage(dev, raw.nbytes)
    try:
        dev.write(st.addr, raw.tobytes())
        st.wrote(FLAT)
        src = Operand("full", pitch=19, offset=3, rows=5)
        addr = forms.packed(dev, st, np.float16, src, 5, 13, 8, 32, "b", 2, 1)
        expected = np.zeros((8, 32), np.float16)
        for row in range(5):
            expected[row, :13] = raw[3 + row * 19 : 16 + row * 19]
        assert dev.read(addr, 256) == reference(expected, "b", 2, 1)
    finally:
        st.free()


def test_rust_generated_checked_quant(dev, monkeypatch):
    monkeypatch.setattr(
        dev, "machine", machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    )
    raw = np.arange(384, dtype=np.float16).reshape(6, 64) / 64
    xa, qa = dev.alloc(raw.nbytes), dev.alloc(512)
    src = Operand("full", pitch=64, offset=16, rows=5)
    args = dict(src=asdict(src), rows=5, cols=48, rp=8, cp=64, side="a", g=2, nk=2)
    try:
        dev.write(xa, raw.tobytes())
        generated.launch(dev, "matmul.quant_kernel", args, {"x": xa, "q": qa})
        expected = np.zeros((8, 64), np.float16)
        expected[:5, :48] = raw[:5, 16:64]
        assert dev.read(qa, 512) == reference(mxfp7.lift(expected, 2), "a", 2, 2)
    finally:
        dev.free(xa)
        dev.free(qa)


@pytest.mark.parametrize("pad_strategy", ["staged", "native"])
def test_padded_scratch_full_readback_and_guards(dev, monkeypatch, pad_strategy):
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    raw = np.arange(384, dtype=np.float16).reshape(6, 64) / 64
    pair = matmul.quant_kernel(
        Operand("full", pitch=64, offset=16, rows=5),
        5,
        48,
        8,
        64,
        "a",
        2,
        2,
        finite=True,
        pad_strategy=pad_strategy,
    )
    c = K.specialise(*pair, machine=machine)
    allocate, free = dev.alloc, dev.free
    scratch = {}
    xa, qa = allocate(raw.nbytes), allocate(512)

    def guarded(nbytes, *args, **kw):
        if not kw.get("owner", "").startswith("scratch:"):
            return allocate(nbytes, *args, **kw)
        base = allocate(nbytes + 256, *args, **kw)
        dev.write(base, bytes([0xD3]) * (nbytes + 256))
        scratch[base + 128] = (base, nbytes)
        return base + 128

    def defer(address):
        if address not in scratch:
            free(address)

    monkeypatch.setattr(dev, "alloc", guarded)
    monkeypatch.setattr(dev, "free", defer)
    try:
        dev.write(xa, raw.tobytes())
        dev.run(c, {"x": xa, "q": qa})
        expected = np.zeros((8, 64), np.float16)
        expected[:5, :48] = raw[:5, 16:64]
        assert dev.read(qa, 512) == reference(mxfp7.lift(expected, 2), "a", 2, 2)
        assert dev.read(xa, raw.nbytes) == raw.tobytes()
        # L2 packing.md: native padding stages nothing in DRAM; staged fills
        # one padded FP16 scratch.
        assert len(scratch) == (pad_strategy == "staged")
        for address, (base, nbytes) in scratch.items():
            assert nbytes == expected.nbytes
            assert dev.read(address, nbytes) == expected.tobytes()
            assert dev.read(base, 128) == bytes([0xD3]) * 128
            assert dev.read(address + nbytes, 128) == bytes([0xD3]) * 128
    finally:
        for base, _ in scratch.values():
            free(base)
        free(xa)
        free(qa)


def test_all_finite_encodings_packed_bytes(dev):
    bits = np.arange(65536, dtype="<u2")
    bits = bits[(bits & 0x7C00) != 0x7C00]
    x = bits.reshape(-1, 32).view(np.float16)
    for side in ("a", "b"):
        actual, _ = trial(dev, x, side, 8, 1, "vector_mx7")
        assert actual == reference(x, side, 8, 1)
