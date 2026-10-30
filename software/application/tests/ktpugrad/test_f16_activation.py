"""An f16 A quantised in the contraction kernel (`KTPU_MM_ACTIVATION_FP16`):
the product binds its f16 rows (`forms.f16_rows`) for a kernel taking them
and quantises no A packing first; the card computes the bytes it computes
from the packing."""

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM

RNG = np.random.default_rng(6)
X = RNG.standard_normal((40, 64)).astype(np.float16)
W = (RNG.standard_normal((96, 64)) / 8).astype(np.float16)
B = RNG.standard_normal(96).astype(np.float16)

#: Each knob set the f16 A is quantised under: first, or in a quant ring.
KNOBS = {
    "first": {"KTPU_MM_ACTIVATION_FP16": "1"},
    "ring1": {
        "KTPU_MM_ACTIVATION_FP16": "1",
        "KTPU_MM_RING_DEPTH": "2",
        "KTPU_MM_QUANT_RING": "1",
    },
    "ring2": {
        "KTPU_MM_ACTIVATION_FP16": "1",
        "KTPU_MM_RING_DEPTH": "2",
        "KTPU_MM_QUANT_RING": "2",
    },
}


@pytest.fixture(autouse=True)
def vector_pairs(monkeypatch):
    """No knob override; K pairs on the vector cores, on any board."""
    for env, _ in MM.KNOBS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("KTPU_MX7_PAIRING", "vector")


def T(a):
    return Tensor(a, device="KTPU")


def linear(monkeypatch, offline=None) -> tuple:
    """``(bytes, A packings)`` of a linear layer with bias and ReLU (no
    bytes on an `offline` device)."""
    seen = []
    mx7 = FM.mx7

    def counted(dev, s, dtype, src, rows, cols, mm, side, batches):
        seen.append(side)
        return mx7(dev, s, dtype, src, rows, cols, mm, side, batches)

    monkeypatch.setattr(FM, "mx7", counted)
    y = (T(X) @ T(W).T + T(B)).relu()
    if offline is None:
        got = y.numpy().tobytes()
    else:
        y.realize()
        offline.drain()
        got = None
    monkeypatch.setattr(FM, "mx7", mx7)
    return got, seen.count("a")


def offline(monkeypatch) -> int:
    dev = D.Device(kind="offline", board="multimesh_v9t6")
    with D.attached(dev):
        return linear(monkeypatch, dev)[1]


@pytest.mark.parametrize("name", list(KNOBS))
def test_an_f16_a_is_bound_unpacked(monkeypatch, name):
    """Under the knob the product packs B only; with `forms.f16_rows`
    answering None it packs A again."""
    assert offline(monkeypatch) == 1
    for env, v in KNOBS[name].items():
        monkeypatch.setenv(env, v)
    assert offline(monkeypatch) == 0
    monkeypatch.setattr(FM, "f16_rows", lambda *a, **k: None)
    assert offline(monkeypatch) == 1


@pytest.mark.parametrize("name", list(KNOBS))
def test_an_f16_a_computes_the_packed_bytes_on_the_card(dev, monkeypatch, name):
    want, packed = linear(monkeypatch)
    assert packed == 1
    for env, v in KNOBS[name].items():
        monkeypatch.setenv(env, v)
    got, packed = linear(monkeypatch)
    assert packed == 0
    assert got == want, name
