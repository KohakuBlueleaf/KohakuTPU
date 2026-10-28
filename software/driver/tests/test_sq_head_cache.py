"""A cached observed consumer index saves reads, never invents ring space."""

import pytest

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport


class Memory(MemoryTransport):
    def __init__(self):
        super().__init__()
        self.head_reads = 0

    def read64(self, addr):
        self.head_reads += addr == 0x100000 + L.SQ_HEAD
        return super().read64(addr)


def queue(cached):
    mem = Memory()
    now = [0]

    def idle():
        now[0] += 1

    q = NodeQueue(
        mem,
        0x100000,
        sq_n=3,
        cq_n=3,
        head_cache=cached,
        timeout=2,
        clock=lambda: now[0],
        idle=idle,
    )
    q.init()
    fw = SpecFirmware(mem, q.base)
    fw.boot()
    return mem, q, fw


@pytest.mark.parametrize("cached", [False, True])
def test_consumed_slots_only_and_wrap(cached):
    mem, q, fw = queue(cached)
    for _ in range(5):
        tags = [q.submit(None, op=L.OP_NOP) for _ in range(3)]
        before = mem.read_block(q.base + L.SQ, 3 * L.SQ_BYTES)
        with pytest.raises(TimeoutError):
            q.submit(None, op=L.OP_NOP)
        assert mem.read_block(q.base + L.SQ, 3 * L.SQ_BYTES) == before
        for _ in tags:
            fw.step()
        assert [q.wait(tag).tag for tag in tags] == tags
    assert q.sq_tail == 15


def test_no_consumer_read_until_cached_space_exhausted():
    counts = []
    for cached in (False, True):
        mem, q, _ = queue(cached)
        for _ in range(3):
            q.submit(None, op=L.OP_NOP)
        counts.append(mem.head_reads)
    assert counts == [3, 0]


def test_reset_discards_old_observation():
    _, q, _ = queue(True)
    q._sq_head_seen = 99
    q.init()
    assert q._sq_head_seen == 0


def test_invented_room_mutant_is_caught(monkeypatch):
    monkeypatch.setattr(NodeQueue, "_sq_room", lambda self: True)
    with pytest.raises(pytest.fail.Exception):
        test_consumed_slots_only_and_wrap(True)


def test_no_cache_mutant_is_caught(monkeypatch):
    original = NodeQueue._sq_room

    def uncached(self):
        self.head_cache = False
        return original(self)

    monkeypatch.setattr(NodeQueue, "_sq_room", uncached)
    with pytest.raises(AssertionError):
        test_no_consumer_read_until_cached_space_exhausted()


def test_bad_mode_refused(monkeypatch):
    monkeypatch.setenv("KOHAKUTPU_SQ_HEAD_CACHE", "2")
    with pytest.raises(ValueError, match="must be 0 or 1"):
        NodeQueue(MemoryTransport(), 0x100000)
