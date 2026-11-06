"""Temporary ownership covers planning and table production as well as launches."""

import pytest
from tinygrad.dtype import dtypes
from tinygrad.uop.ops import AxisType, UOp

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad.planning.lower import Access, Plan
from kohakutpu.application.ktpugrad.run import kernels as K
from kohakutpu.application.ktpugrad.run import shape as S


class Escape(BaseException):
    pass


def plan():
    return Plan("vector", None, "owned", 0, dtypes.half, (2,), ())


def fail(*args, **kw):
    raise Escape()


class Memory:
    def __init__(self, fail_at=None):
        self.live = set()
        self.next = 0
        self.fail_at = fail_at

    def alloc(self, size):
        self.next += 1
        if self.next == self.fail_at:
            raise Escape()
        self.live.add(self.next)
        return self.next

    def free(self, at):
        self.live.remove(at)


@pytest.mark.parametrize("producer", ["materialise", "narrow", "reordered"])
def test_planning_failure_releases_acquired_temporary(monkeypatch, producer):
    dev = Memory()
    plan = Plan(
        "vector",
        None,
        "owned",
        0,
        dtypes.half,
        (1,),
        (),
        expr=X.Op("add", (X.Load(0), X.Load(1))),
        accesses=[Access(1, dtypes.half, (), 0), Access(2, dtypes.half, (), 0)],
    )
    if producer == "materialise":
        monkeypatch.setattr(K, "_leaf_reduce", lambda p: p.expr)
        monkeypatch.setattr(K, "_leaf", lambda p, leaf: (p, (), None))
        monkeypatch.setattr(K, "_rebuild", fail)
    elif producer == "narrow":
        monkeypatch.setattr(K, "_mult", fail)
    else:
        monkeypatch.setattr(K, "_dense", fail)
    with pytest.raises(Escape):
        if producer == "reordered":
            K._reordered(dev, plan, {}, [])
        else:
            getattr(K, "_" + producer)(dev, plan, {})
    assert not dev.live


@pytest.mark.parametrize("producer", ["pad_rows", "laying"])
def test_table_production_failure_releases_its_output(monkeypatch, producer):
    dev = Memory()
    row = UOp.range(2, 0, AxisType.LOOP)
    acc = Access(None, dtypes.half, (), 0)
    monkeypatch.setattr(K, "_read_id", lambda *args: ())
    monkeypatch.setattr(K, "_held", lambda *args: None)
    monkeypatch.setattr(K, "_table_over", fail)
    monkeypatch.setattr(S, "_active_scope", lambda dev: None)
    monkeypatch.setattr(S, "_table_over", fail)
    with pytest.raises(Escape):
        if producer == "pad_rows":
            K._pad_rows(dev, plan(), {}, acc, [], row, [], 2, 1)
        else:
            S._laying(dev, plan(), acc, [row], [], 1, {}, 0)
    assert not dev.live


def test_dense_layout_allocation_failure_releases_pitched_output(monkeypatch):
    dev = Memory(fail_at=2)
    row = UOp.range(2, 0, AxisType.LOOP)
    monkeypatch.setattr(S, "_active_scope", lambda dev: None)
    monkeypatch.setattr(S, "elide_rule", lambda name: False)
    with pytest.raises(Escape):
        S._laying(dev, plan(), Access(1, dtypes.half, (), 0), [row], [], 2, {}, 0)
    assert not dev.live


def test_laid_read_publication_failure_releases_produced_storage(monkeypatch):
    dev = Memory()
    monkeypatch.setattr(S, "_read_id", lambda *args: ())
    monkeypatch.setattr(S, "_held", lambda *args: None)
    monkeypatch.setattr(S, "_laying", lambda *args: (S.Storage(dev, 32), []))
    monkeypatch.setattr(S, "_hold", fail)
    with pytest.raises(Escape):
        S._laid_read(dev, plan(), Access(1, dtypes.half, (), 0), [], [], 1, {}, 0)
    assert not dev.live
