"""Seam schedule recipes retain identities, not allocation lifetimes."""

import pytest
from tinygrad.dtype import dtypes
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp

from kohakutpu.application.ktpugrad import eager_schedule as E
from kohakutpu.stack import rs


def call(size=16, dtype=dtypes.half, layout="flat"):
    ast = UOp(Ops.SINK, arg=layout)
    return ast.call(UOp.new_buffer("KTPU", size, dtype))


def producer(seen):
    def create(sink):
        seen.append(sink.src[0])
        temporary = UOp.new_buffer("KTPU", sink.src[1].max_numel(), sink.src[1].dtype)
        return UOp(Ops.LINEAR, src=(sink.src[0].call(sink.src[1], temporary),)), {}

    return create


def test_schedule_reuses_ast_but_makes_fresh_temporaries():
    table, seen = E.Schedules(), []
    first, _ = table.run(call(), producer(seen), ("machine", "library"))
    sink = call()
    second, _ = table.run(sink, producer(seen), ("machine", "library"))
    assert len(seen) == 1
    assert second.src[0].src[0] is first.src[0].src[0]
    assert second.src[0].src[1] is sink.src[1]
    assert second.src[0].src[2] is not first.src[0].src[2]


def test_compiled_schedule_is_prepared_once_and_uses_fresh_arguments():
    table, seen, compiled = E.Schedules(), [], []

    def prepare(linear):
        compiled.append(linear)
        return linear

    table.run(call(), producer(seen), ("exec",), prepare)
    sink = call()
    result, _ = table.run(sink, producer(seen), ("exec",), prepare)
    assert len(compiled) == 1
    assert result.src[0].src[1] is sink.src[1]
    assert result.src[0].src[2] is not compiled[0].src[0].src[2]


def layout_witness(table):
    seen = []
    for sink in (
        call(),
        call(layout="transposed"),
        call(size=32),
        call(dtype=dtypes.float),
    ):
        linear, _ = table.run(sink, producer(seen), ("machine", "library"))
        assert linear.src[0].src[0] is sink.src[0], "stale layout AST"
        assert linear.src[0].src[2].shape == sink.src[1].shape, "stale buffer size"
        assert linear.src[0].src[2].dtype == sink.src[1].dtype, "stale dtype"
    assert len(seen) == 4


def test_schedule_layout_shape_and_dtype_are_exact():
    layout_witness(E.Schedules())


def test_mutant_schedule_ignores_layout_is_caught():
    assert "app.schedule/ignores_layout" in rs.mutants()
    with (
        rs.mutant("app.schedule/ignores_layout"),
        pytest.raises(AssertionError, match="stale layout"),
    ):
        layout_witness(E.Schedules())


def invalidation_witness(table):
    seen = []
    for context in (("m1", "l1"), ("m1", "l2"), ("m2", "l2")):
        table.run(call(), producer(seen), context)
    assert len(seen) == 3, "library/machine change reused a schedule"
    table.clear()
    table.run(call(), producer(seen), ("m2", "l2"))
    assert len(seen) == 4


def test_schedule_library_machine_invalidation_and_bound(monkeypatch):
    invalidation_witness(E.Schedules())
    monkeypatch.setattr(E, "KEPT", 2)
    table = E.Schedules()
    for size in (16, 32, 64):
        table.run(call(size), producer([]), ("m", "l"))
    assert len(table.entries) == 2


def test_mutant_missing_library_invalidation_is_caught():
    assert "app.schedule/ignores_context" in rs.mutants()
    with (
        rs.mutant("app.schedule/ignores_context"),
        pytest.raises(AssertionError, match="library/machine"),
    ):
        invalidation_witness(E.Schedules())
