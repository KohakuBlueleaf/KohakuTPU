"""The behaviour model's mover against the expectations the Verilated card
meets (`tests/test_l1_examples.py`): the hand-written mover examples, packaged
as for the card, run offline on the model."""

import numpy as np
import pytest

from kohakutpu.application.runtime.device import OFFLINE_BOARD
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.application.tools.l1 import run
from kohakutpu.compiler.l2 import pipeline
from kohakutpu.compiler.layout import tensor
from kohakutpu.language import examples
from kohakutpu.simulation.behave import card as BC


def _execute(card, built) -> dict:
    for pkg, bindings in built.values():
        BC.run(pkg, bindings, card.mem)
    pkg = next(iter(built.values()))[0]
    return {"cycles": 0, "steps": [], "phases": {}, "pkg": pkg}


@pytest.fixture(scope="module")
def card():
    return OfflineCard(OFFLINE_BOARD, execute=_execute)


def f16(rng, *shape):
    return rng.standard_normal(shape).astype(np.float16)


def test_transpose(card):
    x = f16(np.random.default_rng(1), 8, 256)
    y = run(card, "transpose", {"x": x})["out"]["y"]
    assert np.array_equal(y, x.reshape(8, 16, 16).transpose(1, 0, 2).reshape(16, 128))


def test_pad(card):
    x = f16(np.random.default_rng(2), 256)
    y = run(card, "pad", {"x": x})["out"]["y"]
    assert np.all(y[:16] == 1.0) and np.array_equal(y[16:], x)


def test_mask(card):
    x = f16(np.random.default_rng(3), 256)
    y0 = np.full(256, 7.0, np.float16)
    y = run(card, "mask", {"x": x, "y": y0})["out"]["y"]
    want = y0.copy()
    want[64:192] = x[64:192]
    assert np.array_equal(y, want)


def test_fill(card):
    out = run(card, "fill", {})["out"]
    v = 0x12345678
    for name, width in (("y8", 1), ("y16", 2), ("y32", 4)):
        unit = (v & ((1 << (8 * width)) - 1)).to_bytes(width, "little")
        assert out[name].astype(np.uint8).tobytes() == unit * (64 // width)


def test_gather(card):
    rng = np.random.default_rng(4)
    t = f16(rng, 64, 64)
    idx = np.zeros(16, np.uint32)
    idx[:12] = rng.choice(64, 12, replace=True)
    ins = {"t": t, "idx": idx, "idxf": idx.astype(np.float32)}
    out = run(card, "gather", ins)["out"]
    assert np.array_equal(out["y"], t[idx[:12]])
    assert np.array_equal(out["z"], t[idx[:12]])


def test_l2_mover_quantises_and_tiles(card, tmp_path):
    """The quantiser (A and B packings) and GT4 occupants, byte for byte."""
    m = examples.load(examples.example("convert", "l2"))
    p = tmp_path / "convert.ktpu"
    p.write_text(
        pipeline.compile_l1(m, "convert", card.machine).source, encoding="utf-8"
    )
    x = f16(np.random.default_rng(3), 64, 128)
    out = run(card, str(p), {"x": x}, reps=1)["out"]
    assert bytes(out["a"]) == tensor.pack_a(x, 8, 2)
    assert bytes(out["b"]) == tensor.pack_b(x, 16, 4)
    assert np.array_equal(out["t"], tensor.fp16_values(x.view(np.uint16)))


def test_scatter(card):
    rng = np.random.default_rng(5)
    s = f16(rng, 12, 64)
    idx = np.zeros(16, np.uint32)
    idx[:12] = rng.choice(64, 12, replace=False)
    t0 = np.full((64, 64), -3.0, np.float16)
    t = run(card, "scatter", {"s": s, "idx": idx, "t": t0})["out"]["t"]
    want = t0.copy()
    want[idx[:12]] = s
    assert np.array_equal(t, want)
