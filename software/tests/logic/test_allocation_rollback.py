"""Allocation ownership survives painting, pinning and ring publication failures."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime.card.measure import RING_OFF, Ring
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.application.runtime.memory.placement import Placement


class Escape(BaseException):
    pass


def fail(*args, **kw):
    raise Escape()


@pytest.mark.parametrize("method", ["alloc", "carve"])
def test_paint_failure_returns_acquired_extent(monkeypatch, method):
    monkeypatch.setenv("KTPU_NATIVE_MEMORY", "0")
    memory = CardMemory((0x1000, 0x10000), {}, paint=fail)
    with pytest.raises(Escape):
        getattr(memory, method)(64)
    assert not memory.dram.live
    memory.paint = None
    at = memory.alloc(0xF000)
    memory.free(at)


@pytest.mark.parametrize("fault", ["resolve", "retain", "references", "slots"])
def test_placement_failure_releases_owner_and_acquired_pin(fault):
    owners, pins = set(), set()

    def allocate(*args):
        owners.add(100)
        return 100

    arena = SimpleNamespace(retain=pins.add, unretain=pins.remove)
    dev = SimpleNamespace(
        closed=False,
        _alloc=allocate,
        free=owners.remove,
        mmu=SimpleNamespace(
            resolve=lambda *args, **kw: (SimpleNamespace(arena=arena), 100)
        ),
    )
    placement = Placement(dev)
    if fault == "resolve":
        dev.mmu.resolve = fail
    elif fault == "retain":
        arena.retain = fail
    elif fault == "references":

        class References(list):
            def append(self, value):
                raise Escape()

        placement.references = References()
    else:

        class Slots(dict):
            def __setitem__(self, key, value):
                raise Escape()

        placement.slots = Slots()
    cursors = {}
    with pytest.raises(Escape):
        placement.alloc("site", cursors, 32, "dram", None, "runtime")
    assert not owners
    assert not pins
    assert not placement.references
    assert not placement.slots
    assert not cursors


@pytest.mark.parametrize("fault", ["header", "allocation", "publication"])
def test_measurement_failure_unpublishes_before_releasing_rings(fault):
    queues = {n: SimpleNamespace(base=n * 0x10000, size=8192) for n in range(2)}
    words = {q.base + RING_OFF + 8 * i: 0 for q in queues.values() for i in range(3)}
    before = dict(words)
    live = set()
    allocated = 0
    written = 0
    ring = object.__new__(Ring)

    class RefusingAddresses(dict):
        def __setitem__(self, key, value):
            raise Escape()

    def allocate(*args, **kw):
        nonlocal allocated
        allocated += 1
        if fault == "allocation" and allocated == 2:
            raise Escape()
        at = 0x100000 + allocated * 0x100000
        live.add(at)
        if fault == "publication":
            ring.at = RefusingAddresses()
        return at

    def write(at, value):
        nonlocal written
        written += 1
        if fault == "header" and written == 1:
            raise Escape()
        words[at] = value

    def release(at):
        assert all(
            words[q.base + RING_OFF] != at or words[q.base + RING_OFF + 8] == 0
            for q in queues.values()
        )
        live.remove(at)

    card = SimpleNamespace(
        queues=queues,
        mem=SimpleNamespace(read64=words.__getitem__, write64=write),
        memory=SimpleNamespace(alloc=allocate, free=release),
    )
    with pytest.raises(Escape):
        Ring.__init__(ring, card)
    assert not live
    assert words == before
    assert all(q.size == 8192 for q in queues.values())
