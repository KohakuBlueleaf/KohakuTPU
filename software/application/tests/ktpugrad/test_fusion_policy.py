"""One witness per fusion-budget rule and the example realization boundary."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from examples.tinygrad import boundaries, fusion_measure, fusion_replay, vit_tiny
from tinygrad import Tensor
from tinygrad.dtype import dtypes
from tinygrad.helpers import Context
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp

from kohakutpu.application.ktpugrad import fusion, seam
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.stack import rs


def test_producer_budget_limits_and_mutation(monkeypatch):
    cost = fusion.ProducerCost(alu=2, reads=2, reductions=0, intermediate_bytes=256)
    budget = fusion.FusionBudget()
    assert budget.cut(cost) is None
    cases = (
        (replace(budget, max_alu=1), cost, "producer_alu"),
        (replace(budget, l1_words=64), cost, "producer_l1_estimate"),
        (replace(budget, imem_words=8), cost, "producer_imem_estimate"),
        (replace(budget, min_saved_bytes=513), cost, "small_intermediate"),
        (budget, replace(cost, reductions=1), "producer_reduction"),
    )

    def witness():
        for policy, producer, reason in cases:
            assert policy.cut(producer) == reason

    witness()
    monkeypatch.setattr(fusion.FusionBudget, "cut", lambda self, cost: None)
    with pytest.raises(AssertionError):
        witness()


def test_bounded_mode_controls_scheduler_edge(monkeypatch):
    a = UOp(Ops.CONST, dtypes.float, arg=2.0)
    b = UOp(Ops.MUL, dtypes.float, (a, a))
    src = UOp(Ops.ADD, dtypes.float, (a, b))
    cost = fusion.ProducerCost.of(src, (16, 32))
    assert cost == fusion.ProducerCost(2, 0, 0, 2048)
    buf = SimpleNamespace(arg=SimpleNamespace(device="KTPU"), shape=(16, 32))
    idx = SimpleNamespace(src=())
    marker = object()
    monkeypatch.setattr(seam.rangeify, "remove_bufferize", lambda *args: marker)
    monkeypatch.setattr(seam, "FUSE_MODE", "bounded")
    monkeypatch.setattr(seam, "FUSE_BUDGET", fusion.FusionBudget(max_alu=1))
    assert seam._remove_bufferize(src, buf, idx) is None
    monkeypatch.setattr(seam, "FUSE_BUDGET", fusion.FusionBudget(max_alu=2))
    assert seam._remove_bufferize(src, buf, idx) is marker
    monkeypatch.setattr(seam, "FUSE_MODE", "0")
    assert seam._remove_bufferize(src, buf, idx) is None
    buf.arg.device = "CPU"
    assert seam._remove_bufferize(src, buf, idx) is marker


def test_vit_lazy_boundary_and_mutation(monkeypatch):
    with Context(DEV="CPU"):
        block = vit_tiny.Block(64)
        x = Tensor.empty(1, 3, 64, device="CPU")
    calls = []
    monkeypatch.setattr(
        Tensor, "realize", lambda self, *a, **k: calls.append(self) or self
    )

    def witness():
        calls.clear()
        block.realize_ops = False
        block(x)
        assert calls == []
        block.realize_ops = True
        block(x)
        assert len(calls) == 13

    witness()
    monkeypatch.setattr(vit_tiny, "boundary", lambda t, eager, **kwargs: t.realize())
    with pytest.raises(AssertionError):
        witness()


def test_block_boundary_cpu_numerics():
    installed = seam.installed()
    seam.uninstall()
    try:
        model = vit_tiny.seeded_model(1, 17, width=64, image_size=16)
        block = model.blocks[0]
        x = Tensor(
            np.random.default_rng(3).normal(size=(1, 3, 64)).astype(np.float32),
            device="CPU",
        )
        block.realize_ops = True
        eager = block(x).numpy()
        block.realize_ops = False
        lazy = block(x).numpy()
        np.testing.assert_allclose(lazy, eager, rtol=1e-5, atol=1e-6)
    finally:
        if installed:
            seam.install()


def test_training_boundary_preserves_backward_and_mutation(monkeypatch):
    def witness():
        with Context(TRAINING=1):
            x = Tensor([1.0, 2.0], device="CPU").is_param_(True)
            y = boundaries.boundary(x * 3, True)
            (y * y).sum().backward()
            assert x.grad is not None
            np.testing.assert_array_equal(x.grad.numpy(), [18.0, 36.0])

    witness()
    monkeypatch.setattr(boundaries, "boundary", lambda t, eager: t.realize())
    with pytest.raises(AssertionError):
        witness()


def test_replay_preserves_read_order_and_mutation():
    events = [
        ["write", 0, "003c"],
        ["read", 0, 2],
        ["write", 0, "0040"],
        ["read", 0, 2],
    ]

    def witness(events):
        memory = {}

        def replay(log):
            for _, at, raw in log:
                memory[at] = raw
            return []

        card = SimpleNamespace(replay=replay, get=lambda at, size: memory[at])
        _, values = fusion_replay.replay_events(card, events, [((1,), "float16")] * 2)
        assert values == [[1.0], [2.0]]

    witness(events)
    with pytest.raises(AssertionError):
        witness([events[0], events[2], events[1], events[3]])


def test_replay_read_ignores_alignment_padding_and_mutation(monkeypatch):
    def witness():
        raw = np.array([1, 2, 3, 4] + [99] * 12, dtype=np.float16).tobytes()
        got = fusion_replay.values_from_read(raw, (4,), "float16")
        np.testing.assert_array_equal(got, [1, 2, 3, 4])

    witness()
    monkeypatch.setattr(
        fusion_replay,
        "values_from_read",
        lambda raw, shape, dtype: np.frombuffer(raw, dtype=dtype),
    )
    with pytest.raises(AssertionError):
        witness()


def test_replay_reserves_tensor_spans_and_mutation(monkeypatch):
    def witness():
        memory = CardMemory((0x1000, 0x10000), {0: (0x20000, 0x30000)})
        initial = memory.alloc(256)
        memory.alloc(256)
        memory.free(initial)
        fusion_replay.reserve(memory, [0x2000, 0x22000])
        assert memory.alloc(256) >= 0x2000
        assert memory.stage(256, 0) >= 0x22000

    witness()
    monkeypatch.setattr(fusion_replay, "reserve", lambda memory, limits: None)
    with pytest.raises(AssertionError):
        witness()


def test_library_only_probe_has_no_compiler_calls(tmp_path):
    # The measurement module owns no alternate compiler or template library.
    from_args = SimpleNamespace(
        phase="compile",
        record=None,
        graphs=None,
        tokens=197,
        initialized=False,
        workload="probe",
        level="elementwise",
        realize_ops=False,
        board="multimesh_v9t6",
        library=str(tmp_path),
        library_only=False,
        batch=1,
        width=192,
        blocks=1,
    )
    built = fusion_measure.measure(from_args)
    assert built["library_entries"] > 0
    assert built["prebuild_compiler_calls"] > 0
    from_args.library_only = True
    with (
        patch.object(rs, "compile", side_effect=AssertionError("compiler on hit")),
        patch.object(
            rs._rs, "compile", side_effect=AssertionError("native compiler on hit")
        ),
        patch.object(
            rs._rs, "library_prebuild", side_effect=AssertionError("prebuild on hit")
        ),
    ):
        hit = fusion_measure.measure(from_args)
    assert hit["library_entries"] is None
    assert hit["total_compiler_calls"] == 0
    assert hit["counters"]["library_hits"] > 0
    assert hit["device_launches"] == built["device_launches"]
