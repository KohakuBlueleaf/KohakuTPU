"""An f16 A quantised inside the contraction kernel (`KTPU_MM_ACTIVATION_FP16`):
the product binds the producer's f16 rows, packs no A first, and computes the
bytes the packed-A product computes, quantised first or a row tile at a time
into `@stage` slots (`KTPU_MM_QUANT_RING`)."""

import numpy as np
import pytest
from native_mlp_reference import convolution_graph
from native_schedule import graph_cycles, prices
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.api.custom import call as eager_call
from kohakutpu.application.api.tensor import tensor
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels.native import conv as C
from kohakutpu.application.kernels.template import GraphKernel
from kohakutpu.application.runtime import device as card
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.call import call

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
    if name == "ring2" and shape == SHAPES[0]:
        return native_convolution_case(board, monkeypatch)
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


def native_convolution_case(dev, monkeypatch):
    """Local convolution chunks and padded tails preserve the materialized bytes."""

    batch, cout, kh, kw = 2, 32, 3, 3
    chunked = dev.machine.board == "multimesh_v9t6"
    height, width, cin, stride = (20, 29, 32, 1) if chunked else (5, 5, 16, 2)
    oh, ow = (height - 1) // stride + 1, (width - 1) // stride + 1
    rows = batch * oh * ow
    capacity = -(-rows // 4) * 4
    kpad = -(-(kh * kw * cin) // 32) * 32
    rng = np.random.default_rng(48)
    arrays = {
        "p_x": (rng.standard_normal((batch * height * width, cin)) / 4).astype(
            np.float16
        ),
        "c_b": (rng.standard_normal((cout, kpad)) / 8).astype(np.float16),
        "c_in1": (rng.standard_normal(cout) / 4).astype(np.float16),
        "c_in2": (rng.standard_normal((capacity, cout)) / 4).astype(np.float16),
    }
    args = (batch, height, width, cin, cout, kh, kw, dev.machine)
    source = convolution_graph(
        *args, stride=stride, padding=1, activation="relu", residual=True
    )
    expected, baseline = call(dev, GraphKernel(source), arrays)
    kernel = C.kernel(*args, stride=stride, padding=1, activation="relu", residual=True)
    descriptor = kernel.template_call
    assert descriptor is not None
    assert descriptor.generator == ("kernel.sequence" if chunked else "kernel.chain")
    if chunked:
        assert len(descriptor.arguments["members"]) == 2
    got = eager_call(kernel, *(tensor(value) for value in arrays.values()))[0].numpy()
    local_run = dev.runs[-1]
    assert got.tobytes() == expected["c_out"].tobytes()
    before, after = prices(baseline, local_run, machine=dev.machine)
    assert after <= before, (before, after)
    print("native_local_convolution_p1_cycles", {"before": before, "after": after})
    with monkeypatch.context() as patch:
        patch.setattr(
            generated.rs._rs,
            "generate",
            lambda *args: (_ for _ in ()).throw(AssertionError("cache miss")),
        )
        _, cached = generated.invocation(
            dev, descriptor.generator, descriptor.arguments
        )
        assert cached["active_nodes"] == 1


def convolution_schedule_cycles(
    dev,
    *,
    batch=2,
    height=20,
    width=32,
    cin=32,
    cout=32,
    edge_space="stage",
    ring_depth=2,
):
    """Uncollected complete chunk/pressure-case prices; no simulator launch."""

    assert len(dev.card.nodes) == 1
    args = (batch, height, width, cin, cout, 3, 3, dev.machine)
    options: dict = dict(
        padding=1,
        activation="gelu",
        residual=True,
        edge_space=edge_space,
        ring_depth=ring_depth,
    )
    costs = []
    for source in (
        convolution_graph(*args, **options),
        C.kernel(*args, **options).generated,
    ):
        costs.append(graph_cycles(source, dev.machine))
    return {"before": costs[0], "after": costs[1]}
