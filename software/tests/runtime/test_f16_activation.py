"""An f16 A quantised inside the contraction kernel (`KTPU_MM_ACTIVATION_FP16`):
the product binds the producer's f16 rows, packs no A first, and computes the
bytes the packed-A product computes, quantised first or a row tile at a time
into `@stage` slots (`KTPU_MM_QUANT_RING`)."""

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import forms as FM

RNG = np.random.default_rng(6)

#: Each knob set the f16 A is quantised under.
KNOBS = {
    "first": {"KTPU_MM_ACTIVATION_FP16": "1"},
    "ring1": {"KTPU_MM_ACTIVATION_FP16": "1", "KTPU_MM_QUANT_RING": "1"},
    "ring2": {"KTPU_MM_ACTIVATION_FP16": "1", "KTPU_MM_QUANT_RING": "2"},
}

#: (rows, K, N): K of whole K-block pairs, and K 784 padded to 800 (an MXFP7
#: `@stage` scratch of the padded width under the ring).
SHAPES = [(40, 64, 96), (8, 784, 128)]


@pytest.fixture(autouse=True)
def plain_knobs(monkeypatch):
    """No knob override; K pairs on the vector cores, on any board."""
    for env, _ in MM.KNOBS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("KTPU_MX7_PAIRING", "vector")


def T(a) -> Tensor:
    return Tensor(a, device="KTPU")


def linear(monkeypatch, x, w, b) -> tuple:
    """``(bytes, A packings made)`` of a linear layer with bias and ReLU."""
    seen = []
    mx7 = FM.mx7

    def counted(dev, s, dtype, src, rows, cols, mm, side, batches):
        seen.append(side)
        return mx7(dev, s, dtype, src, rows, cols, mm, side, batches)

    monkeypatch.setattr(FM, "mx7", counted)
    got = T(x).matmul(T(w).T).add(T(b)).relu().numpy().tobytes()
    monkeypatch.setattr(FM, "mx7", mx7)
    return got, seen.count("a")


@pytest.fixture(params=["multimesh_v9t6", "multimesh_v9t8"])
def board(request, tmp_path):
    """Both pairings (2+2 packs K pairs, 4+2 single blocks), on an empty
    library: every kernel is compiled by this compiler."""
    ktpugrad.install()
    d = card.Device(kind="double", board=request.param, nodes=1, library=str(tmp_path))
    with card.attached(d):
        yield d
    d.close()


@pytest.mark.parametrize("name", list(KNOBS))
def test_an_f16_a_lowers_to_the_card(monkeypatch, tmp_path, name):
    """Every pass and lowering on the 2+2 board, from an empty library: under
    the ring, K 784 quantises into an MXFP7 `@stage` scratch the `space` pass
    sizes by its packing (L2.PASS.004)."""
    for env, v in KNOBS[name].items():
        monkeypatch.setenv(env, v)
    m, k, n = SHAPES[1]
    ktpugrad.install()
    d = card.Device(
        kind="offline", board="multimesh_v9t6", nodes=1, library=str(tmp_path)
    )
    with card.attached(d):
        (
            T(np.ones((m, k), np.float16)) @ T(np.ones((n, k), np.float16)).T
        ).relu().realize()
        d.drain()
    d.close()


@pytest.mark.parametrize("shape", SHAPES, ids=lambda s: "x".join(map(str, s)))
@pytest.mark.parametrize("name", list(KNOBS))
def test_an_f16_a_computes_the_packed_bytes(board, monkeypatch, name, shape):
    m, k, n = shape
    x = RNG.standard_normal((m, k)).astype(np.float16)
    w = (RNG.standard_normal((n, k)) / 8).astype(np.float16)
    b = RNG.standard_normal(n).astype(np.float16)
    want, packed = linear(monkeypatch, x, w, b)
    assert packed == 1
    for env, v in KNOBS[name].items():
        monkeypatch.setenv(env, v)
    got, packed = linear(monkeypatch, x, w, b)
    assert packed == 0
    assert got == want, name
