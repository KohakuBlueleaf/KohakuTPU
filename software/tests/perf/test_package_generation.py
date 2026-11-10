"""A reset must not let a reused queue tag select an earlier upload."""

import json

import record
from test_package_check import MACHINE, Memory, launch

from kohakuaccel.driver.node.queue.client import Completion, NodeError, NodeQueue
from kohakuaccel.driver.node.queue.layout import Layout


def test_stale_generation_does_not_read_or_attribute_an_old_package(tmp_path):
    trace = record.Trace(tmp_path)
    layout = Layout.from_machine(MACHINE)
    memory = Memory()
    queue = NodeQueue(
        memory,
        0,
        layout=layout,
        record=layout.record,
        spill=memory.spill,
        unspill=memory.unspill,
    )
    queue.init()
    launch(trace, {0: queue})
    raw = bytes(range(128))
    queue.upload(raw)
    trace.packages.published(queue, 7, [raw])
    trace.submitted(0)
    old_generation = queue._generation
    queue.init(sysnode_reset=True)  # transport double, not a hardware operation
    assert queue._generation != old_generation
    memory.package_reads.clear()
    completion = Completion(tag=7, status=0x14, detail=0, step=0, cycles=0, value=0)
    trace.packages.failed(queue, NodeError(completion))
    trace.save({})
    check = json.loads((tmp_path / "package-check.json").read_text())["checks"][0]
    assert check["outcome"] == "unavailable"
    assert "seq" not in check and "address" not in check
    assert not memory.package_reads
