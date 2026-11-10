"""An interrupted collection is uncertain, not a reusable retained epoch."""

import pytest
from test_queue_group import MACHINE, Memory

from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.node.queue.group import PreparedQueueGroup
from kohakuaccel.driver.node.queue.layout import Layout


def test_interrupt_quarantines_every_part_before_any_peer_poll(monkeypatch):
    layout = Layout.from_machine(MACHINE)
    memories = [Memory(), Memory()]
    queues = {
        node: NodeQueue(
            memory, 0, layout=layout, record=layout.record, spill=memory.spill
        )
        for node, memory in enumerate(memories)
    }
    for node, queue in queues.items():
        queue.init()
        SpecFirmware(memories[node], 0, layout=layout, record=layout.record).boot()
    prepared = PreparedQueueGroup(
        {node: bytes([node + 1]) * 32 for node in queues}, queues
    )
    token = prepared.submit()

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("recorder interrupted during a list")

    def must_not_poll(*args, **kwargs):
        raise AssertionError("peer was polled after an uncertain interruption")

    monkeypatch.setattr(queues[0], "wait", interrupted)
    monkeypatch.setattr(queues[1], "wait", must_not_poll)
    with pytest.raises(KeyboardInterrupt):
        token.wait()
    assert prepared.poisoned and all(queue._quarantined for queue in queues.values())
    assert all(queue._package_pins for queue in queues.values())
    with pytest.raises(RuntimeError, match="reset"):
        prepared.close()
