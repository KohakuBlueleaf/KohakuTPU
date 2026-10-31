"""Sequence topology is exact; a batch keeps host I/O in order and an error whole."""

from types import SimpleNamespace

import pytest
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp

from kohakutpu.application.ktpugrad.captured.auto_sequence import Sequences, scope
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.eager_batch import Batch, batching


class CardDouble:
    def __init__(self, submit_wire=None, collect=None):
        self.queues = {}
        self.events = []
        self.submitted = []
        self._submit_wire = submit_wire
        self._collect = collect
        self._eager_batch = None
        self.native_direct_suspended = False

    def submit_wire(self, raw, debug):
        if self._eager_batch is not None and not self._eager_batch._submitting:
            return self._eager_batch.wire(raw, debug)
        if self._submit_wire is not None:
            return self._submit_wire(raw, debug)
        self.submitted.append((raw, debug))
        self.events.append("run")
        return {"cycles": 1}

    def collect(self, pending, *args, **kwargs):
        if self._collect is not None:
            return self._collect(pending, *args, **kwargs)
        return pending

    log = None
    write = NodeCard.write
    get = NodeCard.get

    @property
    def mem(self):
        return SimpleNamespace(
            write_block=lambda at, raw: self.events.append("write"),
            read_block=lambda at, nbytes: self.events.append("read"),
        )


def test_a_sequence_is_its_identity_layout_and_aliases():
    table = Sequences()
    ast, other = object(), object()
    a = ((ast, ((0, 16, "f16", 0), (1, 16, "f16", 0))),)
    alias = ((ast, ((0, 16, "f16", 0), (0, 16, "f16", 0))),)
    layout = ((ast, ((0, 16, "f16", 4), (1, 16, "f16", 0))),)
    assert not table.observe(a)
    assert table.observe(a)
    assert not table.observe(alias)
    assert not table.observe(layout)
    assert not table.observe((*a, (other, ())))
    table.clear()
    assert not table.observe(a)


def test_a_scope_reads_the_latched_switch(monkeypatch):
    monkeypatch.setenv("KTPU_AUTO_SEQUENCE", "1")
    runtime = SimpleNamespace(
        auto_sequence=False,
        recording=False,
        eager_sequences=None,
        card=SimpleNamespace(queues={}),
    )
    scope(runtime, UOp(Ops.LINEAR, src=()), {})
    assert runtime.eager_sequences is None


def test_a_cancelled_batch_publishes_nothing_and_keeps_the_error():
    card = CardDouble()
    with pytest.raises(RuntimeError, match="cancel capture"), batching(card):
        pending = card.submit_wire({0: (b"wire", ())}, {})
        raise RuntimeError("cancel capture")
    assert card.submitted == []
    with pytest.raises(RuntimeError, match="cancel capture"):
        pending.owner.result(pending)


def test_host_io_never_overtakes_a_batch():
    card = CardDouble()
    with batching(card):
        first = card.submit_wire({0: (b"wire", ())}, {})
        card.write(0, b"host")
        second = card.submit_wire({0: (b"wire", ())}, {})
        card.get(0, 4)
    assert card.events == ["run", "write", "run", "read"]
    assert first.owner.result(first)["cycles"] == 1
    assert second.owner.result(second)["cycles"] == 1


def test_a_failed_flush_keeps_its_error():
    failure = OSError("uncertain wire publication")

    def submit(*args):
        raise failure

    def collect(pending):
        pytest.fail("a failed flush produced a phantom pending")

    batch = Batch(CardDouble(submit_wire=submit, collect=collect))
    pending = batch.wire({0: (b"wire", ())}, {})
    with pytest.raises(OSError) as first:
        batch.flush()
    with pytest.raises(OSError) as second:
        batch.result(pending)
    assert first.value is failure and second.value is failure
