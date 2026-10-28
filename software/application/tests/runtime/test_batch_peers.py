"""All communicating nodes must be published before the first node is waited."""

from unittest.mock import patch

import pytest

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.application.runtime.card import NodeCard, Pending


class Arrived(Exception):
    pass


def test_partial_batch_publishes_all_peers():
    card = object.__new__(NodeCard)
    card.queues = {}
    card.timeout = 1
    for n in (0, 1):
        q = NodeQueue(MemoryTransport(), 0x100000)
        q.init()
        q.batch_size = 64
        q.submit(None, op=L.OP_NOP)
        card.queues[n] = q
    first = card.queues[0]

    def wait(*args, **kw):
        assert all(q.mem.read64(q.base + L.SQ_TAIL) == 1 for q in card.queues.values())
        raise Arrived

    pending = Pending(False, {n: (1, None) for n in (0, 1)}, {})
    with patch.object(first, "wait", wait), pytest.raises(Arrived):
        card._collect(pending, False, None)


def test_unpublished_peer_mutant_is_caught(monkeypatch):
    original = NodeQueue.flush
    seen = [0]

    def one_only(self):
        seen[0] += 1
        if seen[0] == 1:
            return original(self)

    monkeypatch.setattr(NodeQueue, "flush", one_only)
    with pytest.raises(AssertionError):
        test_partial_batch_publishes_all_peers()
