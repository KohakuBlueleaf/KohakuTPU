"""HOSTL retains >256 parts and reports the actual late failing list index."""

import struct
from types import SimpleNamespace

import pytest
from test_kaos_sequence import Sequence, Stage
from test_os_parts import Pins
from test_queue_group import MACHINE, Memory

from kohakuaccel.driver.node.queue.client import NodeError, NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.node.queue.layout import Layout
from kohakutpu.application.runtime.split import (
    NodePart,
    PreparedFirmwareGraph,
    SlotLeases,
)


@pytest.mark.parametrize("fail_at", [None, 258])
def test_firmware_retains_long_part_list_and_quarantines_late_failure(fail_at):
    layout = Layout.from_machine(MACHINE)
    memory = Memory()
    queue = NodeQueue(
        memory, 0, layout=layout, record=layout.record, spill=memory.spill
    )
    queue.init()
    executed = []
    state = [0]
    bad = next(
        code for name, code in layout.status.items() if name.upper() == "BAD_STEP"
    )

    def execute(raw, binds):
        index = struct.unpack_from("<Q", raw)[0]
        if index == fail_at:
            return SimpleNamespace(status=bad, sent=0, time={})
        executed.append(index)
        state[0] += index
        return SimpleNamespace(status=layout.ok, sent=1, time={})

    firmware = SpecFirmware(
        memory,
        0,
        interp=SimpleNamespace(run=execute),
        layout=layout,
        record=layout.record,
    )
    firmware.boot()
    queue.idle = firmware.step
    stages = tuple(
        Stage((NodePart(0, struct.pack("<4Q", i, 0, 0, 0)),), i.to_bytes(8, "little"))
        for i in range(1, 261)
    )
    artifact = Sequence(stages[-1].parts, b"long", stages=stages)
    pins, leases = Pins(), SlotLeases()
    prepared = PreparedFirmwareGraph(artifact, {0: queue}, leases, [(pins, 256)])
    uploads = queue.counters["uploads"]
    if fail_at is None:
        try:
            for epoch in range(2):
                assert prepared.submit().wait()[0].ok
                assert executed == list(range(1, 261)) * (epoch + 1)
                assert state[0] == sum(range(1, 261)) * (epoch + 1)
                assert queue.counters["uploads"] == uploads
        finally:
            prepared.close()
        assert pins.held == 0
    else:
        with pytest.raises(NodeError) as caught:
            prepared.submit().wait()
        record = caught.value.completion.record
        assert record is not None and record["list_index"] == fail_at - 1
        assert executed == list(range(1, fail_at))
        assert pins.held == 1 and queue._quarantined
        with pytest.raises(RuntimeError, match="reset"):
            prepared.close()
