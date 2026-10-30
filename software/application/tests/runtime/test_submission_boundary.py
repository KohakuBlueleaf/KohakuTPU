"""Publication begins at the queue write, not at verify/build/upload."""

import struct
from dataclasses import replace
from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as QL
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.application.runtime.device import Device, Recorded
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

MACHINE = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
LAYOUT = QL.Layout.from_machine(MACHINE)
PACKAGE = _rs.package_build('{"credit":32}', "[]", MACHINE, defaults=False)


def kernel(program):
    return SimpleNamespace(
        name="boundary",
        l1=SimpleNamespace(params=lambda *args: []),
        scratch=[{"space": "dram", "bytes": 1024}],
        programs=lambda binds: {None: program},
    )


def program():
    return B.Prebuilt(PACKAGE, (), MACHINE, 0, (), False, (0, 2))


def test_refused_then_normal_submit_on_the_same_device():
    dev = Device(kind="offline", board="multimesh_v9t6")
    at = dev.alloc(32)
    used = dev.dram.used
    bad = replace(
        program(), refused=True, findings=(B.Finding("line", "node 0", "refused"),)
    )
    with pytest.raises(B.Refused):
        dev.submit(kernel(bad), {"x": at})
    assert not dev._uncertain
    assert dev.dram.used == used
    assert dev.dram.references[dev.mmu.entries[at].physical] == 0
    dev.run(kernel(program()), {"x": at})
    dev.write(at, bytes(32))
    dev.free(at)
    dev.close()


def test_prepublication_build_error_rolls_back_too(monkeypatch):
    dev = Device(kind="offline", board="multimesh_v9t6")
    original = B.built

    def fail(*args, **kw):
        raise ValueError("build before publication")

    monkeypatch.setattr(B, "built", fail)
    with pytest.raises(ValueError, match="build before"):
        dev.submit(kernel(program()), {})
    assert not dev._uncertain and dev.dram.used == 0
    monkeypatch.setattr(B, "built", original)
    dev.run(kernel(program()), {})
    dev.close()


def test_marking_uncertain_on_refusal_is_killed(monkeypatch):
    original = Device.submit

    def poisoned(self, *args, **kw):
        try:
            return original(self, *args, **kw)
        except B.Refused:
            self._uncertain.append(None)
            raise

    monkeypatch.setattr(Device, "submit", poisoned)
    with pytest.raises(AssertionError):
        test_refused_then_normal_submit_on_the_same_device()


def test_queue_publication_callback_is_after_upload_before_entry_write():
    mem = MemoryTransport()
    q = NodeQueue(mem, 0x100000, layout=LAYOUT)
    q.init()
    events = []
    original = mem.write_block

    def write(at, raw):
        events.append(("write", at))
        return original(at, raw)

    mem.write_block = write
    q.submit(PACKAGE, publishing=lambda: events.append(("publish", None)))
    publish = events.index(("publish", None))
    entry = ("write", q.base + q.geo["sq"])
    assert 0 < publish < events.index(entry)
    assert events[-1] == ("write", q.base + LAYOUT.sq_tail)


def queued_device(nodes=1):
    dev = Device(kind="offline", board="multimesh_v9t6", nodes=nodes)
    offline = dev.card
    card = object.__new__(NodeCard)
    card.__dict__.update(offline.__dict__)
    card.debug = False
    card._order, card._stdout, card._segs = {}, {}, {}
    card.mem = MemoryTransport()
    card.q = NodeQueue(card.mem, 0x100000, layout=LAYOUT)
    card.q.init()
    card.queues = {0: card.q}
    dev.card = card
    return dev, offline


def test_card_refusal_followed_by_real_queue_publication():
    dev, offline = queued_device()
    bad = replace(
        program(), refused=True, findings=(B.Finding("line", "node 0", "refused"),)
    )
    with pytest.raises(B.Refused):
        dev.submit(kernel(bad), {})
    assert not dev._uncertain and dev.card.publication_epoch == 0
    submitted = dev.submit(kernel(program()), {})
    assert dev.card.q.sq_tail == 1 and dev.card.publication_epoch == 1
    submitted.result = {"cycles": 0}  # Lifetime completion, no arithmetic oracle.
    dev.collect(submitted)
    dev.card = offline
    dev.close()


@pytest.mark.parametrize("phase", ["stdout", "upload", "entry", "tail"])
def test_transport_failure_uses_the_actual_publication_boundary(monkeypatch, phase):
    dev, offline = queued_device()
    card, q = dev.card, dev.card.q
    original = card.mem.write_block

    def fail(*args, **kw):
        raise OSError(phase)

    if phase == "stdout":
        monkeypatch.setattr(q, "read_stdout", fail)
    elif phase == "upload":
        monkeypatch.setattr(q, "upload", fail)
    else:
        address = q.base + (q.geo["sq"] if phase == "entry" else 0x80)

        def write(at, raw):
            if at == address:
                fail()
            return original(at, raw)

        monkeypatch.setattr(card.mem, "write_block", write)
    with pytest.raises(OSError, match=phase):
        dev.submit(kernel(program()), {})
    uncertain = phase in ("entry", "tail")
    assert bool(dev._uncertain) == uncertain
    assert (dev.dram.used > 0) == uncertain
    assert card.publication_epoch == int(uncertain)
    if uncertain:
        with pytest.raises(RuntimeError, match="publication"):
            dev.submit(kernel(program()), {})
    dev.card = offline
    dev.close()


@pytest.mark.parametrize("queued", [False, True])
def test_replay_upload_failure_releases_only_the_attempts_pins(monkeypatch, queued):
    dev, offline = queued_device()
    dev.prepared_replay = False
    at = dev.alloc(32)
    dev.recording = True
    dev._pin_record([at])
    refs = dev._record_pins
    dev.recording = False
    log = Recorded(
        [("run", {0: (PACKAGE, ())})],
        refs,
        dev,
        [("replay", "dsl", {}, refs, refs, (0,))] if queued else (),
    )

    def fail(*args):
        raise OSError("upload")

    if queued:
        monkeypatch.setattr(dev.card.q, "upload", fail)
    else:
        dev.card = offline
        monkeypatch.setattr(offline, "replay", fail)
    with pytest.raises(OSError, match="upload"):
        dev.replay(log)
    assert not dev._uncertain
    entry = dev.mmu.entries[at]
    assert dev.dram.references[entry.physical] == 1
    log.close()
    assert dev.dram.references[entry.physical] == 0
    dev.free(at)
    dev.card = offline
    dev.close()


@pytest.mark.parametrize("phase", ["bindings", "entry-pack"])
def test_queue_preparation_failure_releases_its_package(monkeypatch, phase):
    freed, published = [], []
    mem = MemoryTransport()
    q = NodeQueue(
        mem,
        0x100000,
        spill=lambda size: 0x800000,
        unspill=freed.append,
        layout=LAYOUT,
    )
    q.init()
    if phase == "bindings":
        original = mem.write_block

        def write(at, raw):
            if at == q.base + q.bind_off:
                raise OSError("binding write")
            return original(at, raw)

        monkeypatch.setattr(mem, "write_block", write)
        kwargs, error = {"bindings": [0]}, OSError
    else:
        kwargs, error = {"timeout_cycles": -1}, struct.error
    with pytest.raises(error):
        q.submit(bytes(32), publishing=lambda: published.append(True), **kwargs)
    assert not published and not q._outstanding and not q._package_tags
    assert not q._cache and not q._spilled and freed == [0x800000]


@pytest.mark.parametrize("prior_write", [False, True])
def test_failed_preparation_does_not_record_a_run(monkeypatch, prior_write):
    dev, offline = queued_device()
    dev.record()
    at = dev.alloc(32) if prior_write else None
    if prior_write:
        dev.write(at, bytes(32))
    events, pins, used = list(dev.card.log), list(dev._record_pins), dev.dram.used
    original = dev.card.q.upload

    def fail(*args):
        raise OSError("upload")

    monkeypatch.setattr(dev.card.q, "upload", fail)
    with pytest.raises(OSError, match="upload"):
        dev.submit(kernel(program()), {})
    assert dev.card.log == events and dev._record_runs == []
    assert dev._record_pins == pins and dev.dram.used == used
    monkeypatch.setattr(dev.card.q, "upload", original)
    submitted = dev.submit(kernel(program()), {})
    submitted.result = {"cycles": 0}
    dev.collect(submitted)
    log = dev.recorded()
    assert sum(event[0] == "run" for event in log) == len(log.runs) == 1
    log.close()
    if prior_write:
        dev.free(at)
    dev.card = offline
    dev.close()


def test_second_node_preparation_failure_after_first_publication_is_uncertain(
    monkeypatch,
):
    dev, offline = queued_device(nodes=2)
    q = NodeQueue(dev.card.mem, 0x200000, layout=LAYOUT)
    q.init()
    dev.card.queues[1] = q
    dev.card.nodes = (0, 1)
    dev.card.residents[1] = dev.card.residents[0]

    def fail(*args):
        raise OSError("second node upload")

    monkeypatch.setattr(q, "upload", fail)
    compiled = kernel(program())
    compiled.programs = lambda full: {0: program(), 1: program()}
    with pytest.raises(OSError, match="second node upload"):
        dev.submit(compiled, {})
    assert dev.card.publication_epoch == 1
    assert dev.card.queues[0].sq_tail == 1 and dev.card.queues[1].sq_tail == 0
    assert dev._uncertain and dev.dram.used > 0
    dev.card = offline
    dev.close()


def test_node_card_preparation_does_not_publish(monkeypatch):
    dev = Device(kind="offline", board="multimesh_v9t6")
    card = dev.card
    card.debug = False
    card._submit = lambda *a, **kw: pytest.fail("must not publish")
    monkeypatch.setattr(
        B, "built", lambda *a, **kw: (_ for _ in ()).throw(ValueError("build"))
    )
    with pytest.raises(ValueError, match="build"):
        NodeCard.submit(card, program())
    assert card.publication_epoch == 0
    dev.close()
