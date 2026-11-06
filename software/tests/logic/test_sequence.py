"""Sequence topology is exact; a batch keeps host I/O in order and an error whole."""

from types import SimpleNamespace
from typing import cast

import pytest
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp

from kohakutpu.application.ktpugrad.captured.auto_sequence import Sequences, scope
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.card.submission import Submission as CardSubmission
from kohakutpu.application.runtime.eager_batch import Batch, Deferred, batching


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
    made: list[Deferred] = []
    with pytest.raises(RuntimeError, match="cancel capture"), batching(card):
        made.append(cast(Deferred, card.submit_wire({0: (b"wire", ())}, {})))
        raise RuntimeError("cancel capture")
    assert card.submitted == []
    with pytest.raises(RuntimeError, match="cancel capture"):
        made[0].owner.result(made[0])


def test_host_io_never_overtakes_a_batch():
    card = CardDouble()
    with batching(card):
        first = cast(Deferred, card.submit_wire({0: (b"wire", ())}, {}))
        card.write(0, b"host")
        second = cast(Deferred, card.submit_wire({0: (b"wire", ())}, {}))
        card.get(0, 4)
    assert card.events == ["run", "write", "run", "read"]
    assert first.owner.result(first)["cycles"] == 1
    assert second.owner.result(second)["cycles"] == 1


class QueueDouble:
    def __init__(self):
        self.lists = []

    def prepare(self, data):
        return SimpleNamespace(data=data, close=lambda: None)

    def prepare_list(self, items):
        made = SimpleNamespace(items=items, seconds=0.0, close=lambda: None)
        self.lists.append(made)
        return made


def test_a_list_is_waited_for_with_the_last_duration_of_its_place(monkeypatch):
    card = CardDouble()
    card.queues = {0: QueueDouble()}
    seen = []

    def submit_execution(programs):
        execution, _ = programs[0]
        seen.append(execution.seconds)
        return execution

    def collect(execution, *args, **kwargs):
        execution.seconds += 0.5
        return {"cycles": 7}

    monkeypatch.setattr(card, "submit_execution", submit_execution, raising=False)
    card._collect = collect
    durations: dict = {}
    for step in range(2):
        batch = Batch(card, "seq", durations)
        made = [batch.add({0: (b"k%d" % i, ())}, {0: None}, None) for i in range(3)]
        batch.flush()
        assert [batch.result(m)["cycles"] for m in made] == [7, 0, 0]
    assert len(card.queues[0].lists) == 2 and len(card.queues[0].lists[0].items) == 3
    assert seen == [0.0, 0.5] and durations == {("seq", 0): 1.0}


def test_a_plain_card_run_joins_the_eager_list(monkeypatch):
    built = SimpleNamespace(to_bytes=lambda: b"lowered")
    monkeypatch.setattr(B, "placed", lambda program, nodes: {0: program})
    monkeypatch.setattr(
        B, "built", lambda progs, *a, **k: {0: (built, built, ("bind",))}
    )
    published = []
    added = []
    batch = SimpleNamespace(
        flush=lambda: published.append("flush"),
        add=lambda raw, plain, alone: added.append(raw) or "deferred",
    )
    card = object.__new__(NodeCard)
    card.__dict__.update(
        _eager_batch=batch,
        debug=False,
        nodes=(0,),
        verify=lambda progs: None,
        resident_mode=SimpleNamespace(forget_all=False, stateless=False, resident=True),
        submit_cache=False,
        residents={0: SimpleNamespace(snapshot=lambda: None, restore=lambda s: None)},
        machine=None,
        fetch=None,
        admission_cached=True,
        log=None,
        _submit=lambda *a, **k: published.append("run") or 1,
        _forget_residents=lambda nodes: None,
    )
    assert CardSubmission.submit(cast(CardSubmission, card), "program") == "deferred"
    assert added == [{0: (b"lowered", ("bind",))}] and published == []
    CardSubmission.submit(cast(CardSubmission, card), "program", after=3)
    assert published == ["flush", "run"]


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
