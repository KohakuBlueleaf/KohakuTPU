"""Kernel scratch is one window a space: a replayed graph whose sites' scratch
outgrows its space a span each (ring products' staging, a LeNet training
step's DRAM) is captured, a replayed ring graph computes what its eager
calls compute, and ring products queued back to back compute what each
computes alone; a run over two nodes queues alone."""

import pathlib
import runpy

import numpy as np
import pytest
from examples.tinygrad.mnist_mlp import train_step
from examples.tinygrad.mnist_models import LeNet
from tinygrad import Tensor
from tinygrad.engine.jit import TinyJit
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters

from application.tests.runtime import test_mm_knobs_model as M
from application.tests.runtime import zero_block as Z
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.ktpugrad import inference as I
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.arena import OutOfMemory
from kohakutpu.stack import rs

POC = runpy.run_path(
    str(pathlib.Path(__file__).resolve().parents[2] / "examples/multinode/poc.py")
)
#: The LeNet step's batch: its graph's sites hold 186 MB of DRAM scratch a
#: span each, past the 1n model's 266 MB with its 75 MB of tensors.
BATCH = 32


def per_site(dev):
    """`Device._windowed` as a span a run: each through `alloc` (a graph
    site's own pinned span), never freed."""
    return lambda spans: {
        name: dev.alloc(nbytes, space, node, owner="scratch")
        for name, nbytes, space, node, _ in spans
    }


def chain(layers, seed, d=192):
    """`layers` products of [rows, d] activations by [d, d] weights."""
    rng = np.random.default_rng(seed)
    ws = [
        Tensor((0.1 * rng.standard_normal((d, d))).astype(np.float16), "KTPU")
        for _ in range(layers)
    ]

    def forward(x):
        for w in ws:
            x = (x @ w.T).contiguous().realize()
        return x

    return forward


def inputs(seed, calls, rows=200, d=192):
    rng = np.random.default_rng(seed)
    return [rng.standard_normal((rows, d)).astype(np.float16) for _ in range(calls)]


def lenet_steps(calls):
    """`calls` LeNet training steps at BATCH through `TinyJit` (replayed
    from the fifth call)."""
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = LeNet()
    place(model)
    opt = Adam16(get_parameters(model), lr=2e-3)
    run = TinyJit(lambda xb, yb: train_step(model, opt, xb, yb))
    rng = np.random.default_rng(1)
    eye = np.eye(10, dtype=np.float16)
    for _ in range(calls):
        xb = rng.random((BATCH, 784)).astype(np.float16)
        yb = eye[rng.integers(0, 10, BATCH)]
        with Context(TRAINING=1):
            run(Tensor(xb, "KTPU").realize(), Tensor(yb, "KTPU").realize())


@pytest.mark.parametrize("window", [True, False])
def test_a_ring_graph_over_the_staging_store_is_captured(monkeypatch, window):
    """24 ring products, replayed: each one's stage scratch more than a 24th
    of the staging store. With a span a site (no window) the capture runs
    out of staging."""
    monkeypatch.setenv("KTPU_MM_RING_DEPTH", "2")
    dev = D.Device("offline", board="vlt_v9t6s_1n")
    if not window:
        monkeypatch.setattr(dev, "_windowed", per_site(dev))
    node = dev.card.nodes[0]
    stage = dev.memory.staging[node]
    try:
        with D.attached(dev):
            predict = I.Inference(chain(24, 3), replay=True)
            try:
                for x in inputs(4, 6):
                    predict(Tensor(x, "KTPU"))
                    dev.drain()
            except OutOfMemory:
                assert not window
                return
            finally:
                predict.reset()
        assert window
        assert 24 * dev.windows["stage", node][1] > stage.hi - stage.lo
    finally:
        dev.close()


@pytest.mark.parametrize("window", [True, False])
def test_a_lenet_step_graph_over_dram_is_captured(monkeypatch, window):
    """The LeNet training step at BATCH through `TinyJit`, offline: captured
    and replayed in DRAM. With a span a site (no window) its scratch runs
    out of DRAM."""
    dev = D.Device("offline", board="vlt_v9t6s_1n")
    if not window:
        monkeypatch.setattr(dev, "_windowed", per_site(dev))
    try:
        with D.attached(dev):
            try:
                lenet_steps(6)
            except OutOfMemory:
                assert not window
                return
        assert window
        assert dev.windows["dram", None][1] < 32 << 20
    finally:
        dev.close()


def test_a_replayed_ring_graph_computes_the_eager_calls(dev, monkeypatch):
    """Two chained ring products through `Inference`, seven calls eager and
    seven replayed (`TinyJit` replays from the fifth call), on the card model;
    from the first replay each replayed-mode call followed by an eager ring
    product of a larger shape, which outgrows the stage window the recording
    pins: the larger product runs in a window of its own, freed once it is
    collected, and the later replays run in the old window, which the
    recording keeps live: every call's output, and every larger product's,
    holds the same bytes. (A window grown between the recordings makes them
    differ, and the graph is not replayed.)"""
    monkeypatch.setenv("KTPU_MM_RING_DEPTH", "2")
    forward = chain(2, 5, d=64)
    rng = np.random.default_rng(7)
    big = [
        Tensor(rng.standard_normal(shape).astype(np.float16), "KTPU")
        for shape in ((200, 192), (192, 192))
    ]
    replays, needs = [], []
    keep, keep_window = dev.replay, dev._window
    monkeypatch.setattr(
        dev, "replay", lambda *a, **kw: replays.append(1) or keep(*a, **kw)
    )
    monkeypatch.setattr(
        dev, "_window", lambda key, n: needs.append(n) or keep_window(key, n)
    )
    got, other = {}, []
    window = ("stage", dev.card.nodes[0])
    stage = dev.memory.staging[window[1]]
    for replay in (False, True):
        predict = I.Inference(forward, replay=replay)
        got[replay] = []
        for i, x in enumerate(inputs(6, 7, rows=16, d=64)):
            got[replay].append(predict(Tensor(x, "KTPU")).numpy())
            if replay and i >= 4:
                if i == 4:
                    recorded, size = dev.windows[window]
                    physical = dev.mmu.resolve(recorded)[1]
                    needs.clear()
                other.append((big[0] @ big[1].T).numpy())
                assert window not in dev.windows and physical in stage.live
        predict.reset()
        assert len(replays) == 3 * replay
    assert min(needs) > size
    assert all(
        a.tobytes() == b.tobytes() for a, b in zip(got[False], got[True], strict=True)
    )
    assert len({y.tobytes() for y in other}) == 1


def queued_gradients(dev, launches):
    """`launches` of the two-node POC's local-gradient kernel (stage scratch
    on each node), each its own targets, queued in one plan scope with
    nothing collected until the last: whether each run was alone in flight
    once queued, and whether every gradient is the oracle's."""
    kernel, given = POC["local_gradient_kernel"]()
    compiled = dev.specialise(kernel, given)
    rng = np.random.default_rng(31)
    held, alone = [], []
    with dev.submission_scope("queued"):
        for _ in range(launches):
            arrays = POC["training_data"]()
            for node in range(2):
                arrays[f"y{node}"] = rng.choice([-0.125, 0.125], (32, 32))
                arrays[f"y{node}"] = arrays[f"y{node}"].astype(np.float16)
            binds = {}
            for name, array in arrays.items():
                binds[name] = dev.alloc(array.nbytes)
                dev.write(binds[name], array.tobytes())
            for node in range(2):
                binds[f"g{node}"] = dev.alloc(32 * 32 * 2)
            assert isinstance(dev.run(compiled, binds), D.Submitted)
            alone.append(len(dev.inflight) == 1)
            held.append((arrays, binds))
    dev.drain()
    exact = True
    for arrays, binds in held:
        grads, _, _ = POC["oracle"](arrays)
        for node in range(2):
            got = np.frombuffer(dev.read(binds[f"g{node}"], 32 * 32 * 2), np.float16)
            exact &= np.array_equal(got.reshape(32, 32), grads[node])
        for at in binds.values():
            dev.free(at)
    return alone, exact


def test_two_node_runs_queue_alone_and_compute_the_oracle(dev, monkeypatch):
    """Four two-node runs with stage scratch on both nodes, queued lazily on
    the card tests' device when it has two nodes (the model at `POC["BOARD"]`):
    each is alone in flight (the two nodes' queues are not ordered with each
    other, so a run over both must not overlap another) and every gradient is
    the oracle's. Without `_fence` they overlap."""
    if len(dev.card.nodes) < 2:
        pytest.skip(f"a two-node device ({POC['BOARD']})")
    monkeypatch.setattr(dev, "queued", True)
    alone, exact = queued_gradients(dev, 4)
    assert all(alone) and exact
    monkeypatch.setattr(dev, "_fence", lambda nodes: None)
    alone, _ = queued_gradients(dev, 4)
    assert not all(alone)


#: Ring products queued back to back: two shapes, so the window grows while
#: runs that bound the smaller one are in flight.
QUEUED = [(1, 20, 64, 64), (2, 40, 128, 96)] * 4


def test_queued_ring_products_compute_their_eager_runs(dev, monkeypatch):
    """Eight ring products, each its own operands, submitted in one plan
    scope with nothing collected until the last: every output holds the
    bytes of the same product run and collected alone."""
    mt = rs.machine_json(dev.machine)
    runs = []
    for i, (bt, m, n, k) in enumerate(QUEUED):
        mm = MM.plan(bt, m, n, k, False, machine_json=mt, pad_m=True)
        _, _, data = M.operands(dev, mm, 20 + i)
        out = Operand("full", pitch=n, rows=bt * mm.mp)
        args = M.call(mm, X.Load(0), [], out, ring_depth=2)
        assert MM.resolved(args, mt)["knobs"]["ring_depth"] == 2
        runs.append((args, data, bt * mm.mp, n))
    want = [M.product(dev, *r) for r in runs]
    held, most = [], 0
    with dev.submission_scope("queued"):
        for args, data, rows, n in runs:
            src = M.source(dev, args)
            comp, _ = Z.compiled(dev, src)
            sizes = M.port_bytes(src)
            binds = {name: dev.alloc(size) for name, size in sizes.items()}
            for name, at in binds.items():
                fill = bytes([0xD3]) if name == "out" else bytes(1)
                got = data.get(name, b"")[: sizes[name]]
                dev.write(at, got + fill * (sizes[name] - len(got)))
            assert isinstance(dev.run(comp, binds, kind="matmul"), D.Submitted)
            most = max(most, len(dev.inflight))
            held.append((binds, rows, n))
    assert most >= 2
    dev.drain()
    for (binds, rows, n), expect in zip(held, want, strict=True):
        assert dev.read(binds["out"], rows * n * 2) == expect
        for at in binds.values():
            dev.free(at)
