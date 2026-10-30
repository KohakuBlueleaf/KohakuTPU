"""One witness per dispatch rule, including the disabled-path mutations."""

import sys
from types import SimpleNamespace

import numpy as np
import pytest
from examples.tinygrad import dispatch_measure as measurement
from tinygrad import Tensor
from tinygrad.dtype import dtypes
from tinygrad.uop.ops import AxisType, UOp

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.application.ktpugrad import inference as I
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad.device import KTPUGraph
from kohakutpu.application.ktpugrad.lower import Access, Plan
from kohakutpu.application.runtime import device as D
from kohakutpu.language.dsl import TensorSpec


def test_constant_key_keeps_exact_array_bytes_and_digest_mutant_is_caught(monkeypatch):
    raw = np.arange(64, dtype=np.float16).reshape(8, 8)[:, ::2]

    def witness():
        key = D._const_key(raw)
        assert key == ("ndarray", raw.shape, str(raw.dtype), raw.tobytes())

    witness()
    monkeypatch.setattr(
        D, "_const_key", lambda v: ("ndarray", v.shape, str(v.dtype), b"digest-only")
    )
    with pytest.raises(AssertionError):
        witness()


def test_measurement_requires_a_steady_sample_and_undo_is_caught(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["dispatch_measure", "mlp", "--repeat", "1"])
    monkeypatch.setattr(measurement, "measure", lambda args: None)

    def witness():
        with pytest.raises(SystemExit) as error:
            measurement.main()
        assert error.value.code == 2

    witness()
    monkeypatch.setattr(measurement.argparse.ArgumentParser, "error", lambda *a: None)
    with pytest.raises(pytest.fail.Exception):
        witness()


def test_inference_default_signature_bound_and_disabled_mutant(monkeypatch):
    made = []

    class Jit:
        def __init__(self, fn):
            self.fn = fn
            made.append(self)

        def __call__(self, *args):
            return self.fn(*args)

    monkeypatch.setattr(I, "TinyJit", Jit)
    monkeypatch.setattr(I, "SIGNATURES_KEPT", 2)
    monkeypatch.setenv("KTPU_INFERENCE_REPLAY", "1")
    dev = D.Device("offline", board="multimesh_v9t6")
    with D.attached(dev):

        def witness():
            call = I.Inference(lambda x: x)
            x = Tensor(np.ones((2, 4), np.float32), device="CPU")
            call(x)
            call(x)
            assert len(call.jits) == 1
            for n in (3, 4):
                call(Tensor(np.ones((n, 4), np.float32), device="CPU"))
            assert len(call.jits) == 2
            call.reset()
            assert not call.jits

        witness()
        monkeypatch.setenv("KTPU_INFERENCE_REPLAY", "0")
        with pytest.raises(AssertionError):
            witness()
    dev.close()


def test_specialisation_hit_skips_trace_and_tracks_layout_library(monkeypatch):
    monkeypatch.setenv("KTPU_SPECIALISE_CACHE", "1")
    monkeypatch.setattr(D, "KERNELS_KEPT", 2)
    calls = []

    class Kernel:
        def __init__(self):
            self.consts = {"scale": np.array([1.0], dtype=np.float32)}

        def trace(self, given, **kwargs):
            calls.append(given)
            return SimpleNamespace(graph_json="{}", graph={}, name="test")

    dev = D.Device("offline", board="multimesh_v9t6", library="first")
    monkeypatch.setattr(dev, "kernel", lambda *a: object())
    kernel = Kernel()
    given = {"x": TensorSpec.make((32,))}
    try:
        first = dev.specialise(kernel, given)
        assert dev.specialise(kernel, given) is first and len(calls) == 1
        dev.specialise(kernel, {"x": given["x"].with_(layout={"tile": (4, 8)})})
        dev.library = "second"
        assert dev.specialise(kernel, given) is not first
        assert len(dev.specialisations) == 2
        kernel.consts["scale"][0] = 2
        assert dev.specialise(kernel, given) is not first
        assert len(calls) == 4
        dev.invalidate_library()
        assert not dev.specialisations
        monkeypatch.setenv("KTPU_SPECIALISE_CACHE", "0")
        uncached = D.Device("offline", board="multimesh_v9t6", library="first")
        monkeypatch.setattr(uncached, "kernel", lambda *a: object())
        try:
            with pytest.raises(AssertionError):
                assert uncached.specialise(kernel, given) is uncached.specialise(
                    kernel, given
                )
        finally:
            uncached.close()
    finally:
        dev.close()


def test_operand_recipe_reuses_but_stride_mutation_misses(monkeypatch):
    monkeypatch.setenv("KTPU_PLAN_CACHE", "1")
    R.classify.cache.clear()
    r, c = UOp.range(4, 0, AxisType.LOOP), UOp.range(32, 1, AxisType.LOOP)
    a = Access(1, dtypes.half, ((r, 32), (c, 1)), 0)
    first = R.classify(a, [r], [c])
    assert R.classify(a, [r], [c]) is first
    changed = Access(1, dtypes.half, ((r, 64), (c, 1)), 0)
    assert R.classify(changed, [r], [c]).pitch == 64
    monkeypatch.setenv("KTPU_PLAN_CACHE", "0")
    with pytest.raises(AssertionError):
        assert R.classify(a, [r], [c]) is R.classify(a, [r], [c])


def test_coalesced_recipe_skips_replanning_and_undo_is_caught(monkeypatch):
    monkeypatch.setenv("KTPU_PLAN_CACHE", "1")
    r, c = UOp.range(4, 0, AxisType.LOOP), UOp.range(32, 1, AxisType.LOOP)
    a = Access(1, dtypes.half, ((r, 32), (c, 1)), 0)
    plan = Plan(
        "copy", UOp.sink(), "merge", 0, dtypes.half, (4, 32), (r, c), accesses=[a]
    )
    first = R._coalesce(plan)
    assert first.out_shape == (128,) and R._coalesce(plan) is first
    monkeypatch.setenv("KTPU_PLAN_CACHE", "0")
    with pytest.raises(AssertionError):
        assert R._coalesce(plan) is R._coalesce(plan)


def test_inference_replay_updates_slots_without_entering_planner(monkeypatch):
    graphs = []
    original = KTPUGraph.__call__

    def observe(self, *args, **kw):
        if self not in graphs:
            graphs.append(self)
        return original(self, *args, **kw)

    monkeypatch.setenv("KTPU_INFERENCE_REPLAY", "1")
    monkeypatch.setattr(KTPUGraph, "__call__", observe)
    dev = D.Device("offline", board="multimesh_v9t6")

    def forward(x):
        doubled = (x * 2).contiguous().realize()
        return (doubled + 1).contiguous().realize()

    predict = I.Inference(forward)
    try:
        with D.attached(dev):
            for i in range(7):
                predict(Tensor(np.full(32, i, dtype=np.float16), device="KTPU"))
            assert graphs and all(g.counts["replayed"] > 0 for g in graphs)

            def forbidden(*args, **kw):
                raise RuntimeError("planner entered")

            monkeypatch.setattr(R, "execute", forbidden)
            raw = np.full(32, 17, dtype=np.float16)
            predict(Tensor(raw, device="KTPU"))
            assert all(
                s._buf.host == raw.tobytes() for g in graphs for s in g.slots.values()
            )
            predict.replay = False
            with pytest.raises(RuntimeError, match="planner entered"):
                predict(Tensor(raw + 1, device="KTPU"))
    finally:
        predict.reset()
        graphs.clear()
        dev.close()


def test_eager_scope_publishes_before_next_plan_and_undo_is_caught(monkeypatch):
    monkeypatch.setenv("KOHAKUTPU_EAGER_FLUSH", "1")
    monkeypatch.setenv("KOHAKUTPU_SUBMIT_BATCH", "4")
    dev = D.Device("offline", board="multimesh_v9t6")
    mem = MemoryTransport()
    q = NodeQueue(mem, 0x100000, layout=dev.card.queue_layout)
    q.init()
    dev.card.queues = {0: q}
    try:
        with dev.submission_scope("one"):
            q.submit(bytes(32))
            assert mem.read64(q.base + q.layout.sq_tail) == 0
        assert mem.read64(q.base + q.layout.sq_tail) == 1
        monkeypatch.setenv("KOHAKUTPU_EAGER_FLUSH", "0")
        dev2 = D.Device("offline", board="multimesh_v9t6")
        dev2.card.queues = {0: q}
        try:
            with dev2.submission_scope("two"):
                q.submit(bytes(32))
            with pytest.raises(AssertionError):
                assert mem.read64(q.base + q.layout.sq_tail) == 2
            q.flush()
        finally:
            dev2.card.queues = {}
            dev2.close()
    finally:
        dev.card.queues = {}
        dev.close()
