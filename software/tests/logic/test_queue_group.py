"""A retained group publishes every node before any terminal CQ is collected."""

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue.client import NodeError, NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.node.queue.group import PreparedQueueGroup
from kohakuaccel.driver.node.queue.layout import LIST_F_CHAIN, Layout
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


@pytest.mark.parametrize("failure", [None, "BAD_STEP", "TIMEOUT"])
def test_capacity_list_executes_in_order_and_never_executes_after_middle_failure(
    monkeypatch, failure
):
    """A numeric FIFO witness, not a part-count or list-shape assertion.

    On base tuple packages are rejected at upload, before this outcome can be
    produced. SpecFirmware witnesses the host/list contract, not DE silicon.
    """
    monkeypatch.delenv("KOHAKUTPU_LIST_CHAIN", raising=False)
    layout = Layout.from_machine(MACHINE)
    memory = Memory()
    queue = NodeQueue(
        memory, 0, layout=layout, record=layout.record, spill=memory.spill
    )
    queue.init()
    state = [0]

    def execute(raw, binds):
        multiply, add = struct.unpack_from("<2Q", raw)
        status = layout.ok
        if failure is not None and add == 4:
            status = next(
                code for code, name in queue_status.items() if name == failure
            )
        else:
            state[0] = state[0] * multiply + add
        return SimpleNamespace(status=status, sent=1, time={})

    # Use the generated names, not machine-specific numeric test status values.
    queue_status = {code: name.upper() for name, code in layout.status.items()}
    firmware = SpecFirmware(
        memory,
        0,
        interp=SimpleNamespace(run=execute),
        layout=layout,
        record=layout.record,
    )
    firmware.boot()
    queue.idle = firmware.step
    parts = tuple(
        struct.pack("<4Q", multiply, add, 0, 0)
        for multiply, add in ((2, 1), (3, 4), (2, 7))
    )
    prepared = PreparedQueueGroup({0: parts}, {0: queue})
    assert prepared.lists[0].flags & LIST_F_CHAIN
    uploads = queue.counters["uploads"]
    if failure is not None:
        state[0] = 5
        with pytest.raises(NodeError) as caught:
            prepared.submit().wait()
        assert caught.value.completion.name == failure
        assert caught.value.completion.record is not None
        assert caught.value.completion.record["list_index"] == 1
        assert state[0] == 5 * 2 + 1  # neither middle nor final update happened
        assert prepared.poisoned and queue._quarantined and queue._package_pins
        with pytest.raises(RuntimeError, match="failed"):
            prepared.submit()
        with pytest.raises(RuntimeError, match="reset"):
            prepared.close()
    else:
        try:
            for seed in (1, 5):
                state[0] = seed
                assert prepared.submit().wait()[0].ok
                assert state[0] == ((seed * 2 + 1) * 3 + 4) * 2 + 7
                assert queue.counters["uploads"] == uploads
        finally:
            prepared.close()
