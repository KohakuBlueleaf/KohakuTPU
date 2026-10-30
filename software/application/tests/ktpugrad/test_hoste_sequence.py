"""Sequence topology is exact; cancellation never publishes an unobserved tail."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.ktpugrad.auto_sequence import Sequences
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.eager_batch import Batch, batching
from kohakutpu.stack import rs


class CardDouble:
    def __init__(self, events=None, submitted=None, submit_wire=None, collect=None):
        self.queues = {}
        self.events = events if events is not None else []
        self.submitted = submitted if submitted is not None else []
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

    def submit(self, *args):
        if self._eager_batch is not None:
            self._eager_batch.flush()
        self.events.append("immediate")

    log = None
    write = NodeCard.write
    get = NodeCard.get

    @property
    def mem(self):
        return SimpleNamespace(
            write_block=lambda at, raw: self.events.append("write"),
            read_block=lambda at, nbytes: self.events.append("read"),
        )


def sequence_witness(table):
    ast, other = object(), object()
    a = ((ast, ((0, 16, "f16", 0), (1, 16, "f16", 0))),)
    alias = ((ast, ((0, 16, "f16", 0), (0, 16, "f16", 0))),)
    layout = ((ast, ((0, 16, "f16", 4), (1, 16, "f16", 0))),)
    assert not table.observe(a)
    assert table.observe(a)
    assert not table.observe(alias), "alias divergence accepted"
    assert not table.observe(layout), "layout divergence accepted"
    assert not table.observe((*a, (other, ()))), "sequence tail guessed"
    table.clear()
    assert not table.observe(a), "owner invalidation lost"


def test_sequence_identity_layout_alias_and_epoch():
    sequence_witness(Sequences())


def test_mutant_sequence_ignores_layout_is_caught():
    assert "app.sequence/ignores_layout" in rs.mutants()
    with (
        rs.mutant("app.sequence/ignores_layout"),
        pytest.raises(AssertionError, match="alias divergence"),
    ):
        sequence_witness(Sequences())


def test_cancelled_sequence_does_not_publish_and_retains_error():
    card = CardDouble()
    with pytest.raises(RuntimeError, match="cancel capture"), batching(card):
        pending = card.submit_wire({0: (b"wire", ())}, {})
        raise RuntimeError("cancel capture")
    assert card.submitted == []
    with pytest.raises(RuntimeError, match="cancel capture"):
        pending.owner.result(pending)


def io_boundary_witness():
    card = CardDouble()
    with batching(card):
        first = card.submit_wire({0: (b"wire", ())}, {})
        card.write(0, b"host")
        second = card.submit_wire({0: (b"wire", ())}, {})
        card.get(0, 4)
    assert card.events == [
        "run",
        "write",
        "run",
        "read",
    ], "host I/O overtook buffered work"
    assert first.owner.result(first)["cycles"] == 1
    assert second.owner.result(second)["cycles"] == 1


def test_sequence_flushes_before_host_read_and_write():
    io_boundary_witness()


def test_mutant_skipped_host_write_barrier_is_caught():
    assert "app.batch/write_skips_flush" in rs.mutants()
    with (
        rs.mutant("app.batch/write_skips_flush"),
        pytest.raises(AssertionError, match="host I/O"),
    ):
        io_boundary_witness()


def test_failed_flush_keeps_original_error_not_phantom_pending():
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
