"""A retained group publishes every node before any terminal CQ is collected."""

from pathlib import Path

import pytest

from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.node.queue.group import PreparedQueueGroup
from kohakuaccel.driver.node.queue.layout import Layout
from kohakutpu.application.runtime.offline import HostMemory

MACHINE = Path(__file__).resolve().parents[3] / "machines" / "multimesh_v9t6.json"


class Memory(HostMemory):
    fail_at: int | None = None
    next_address = 1 << 22

    def spill(self, size):
        at = self.next_address
        self.next_address += (size + 255) // 256 * 256
        return at

    def write_block(self, addr: int, data: bytes) -> None:
        if addr == self.fail_at:
            raise OSError("second node publication failed")
        super().write_block(addr, data)


def test_group_publishes_all_retained_lists_and_quarantines_partial_publication():
    layout = Layout.from_machine(MACHINE)
    memories = [Memory(), Memory()]
    queues = {
        node: NodeQueue(mem, 0, layout=layout, record=layout.record, spill=mem.spill)
        for node, mem in enumerate(memories)
    }
    firmware = []
    for node, queue in queues.items():
        queue.init()
        fw = SpecFirmware(memories[node], 0, layout=layout, record=layout.record)
        fw.boot()
        firmware.append(fw)

    def progress():
        tails = [mem.read64(layout.sq_tail) for mem in memories]
        assert tails[0] == tails[1] and tails[0] > 0
        for fw in firmware:
            fw.step()

    for queue in queues.values():
        queue.idle = progress
    packages = {node: bytes([node + 1]) * 32 for node in queues}
    prepared = PreparedQueueGroup(packages, queues)
    uploads = [q.counters["uploads"] for q in queues.values()]
    for _ in range(2):
        token = prepared.submit()
        with pytest.raises(RuntimeError, match="already in flight"):
            prepared.submit()
        assert all(c.ok for c in token.wait().values())
        assert [q.counters["uploads"] for q in queues.values()] == uploads
    memories[1].fail_at = layout.sq_tail
    with pytest.raises(OSError, match="second node"):
        prepared.submit()
    assert all(q._quarantined for q in queues.values())
    assert all(q._package_pins for q in queues.values())
    with pytest.raises(RuntimeError, match="reset"):
        prepared.close()
