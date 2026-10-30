"""A completion published during the last idle before a deadline is consumed."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport

LAYOUT = L.Layout.from_machine(
    Path(__file__).resolve().parents[3] / "machines/multimesh_v9t6.json"
)


def wait_through_deadline():
    mem, now = MemoryTransport(), [0.0]
    q = NodeQueue(mem, 0x100000, clock=lambda: now[0], layout=LAYOUT)
    q.init()
    fw = SpecFirmware(mem, q.base, layout=LAYOUT)
    fw.boot()
    tag = q.submit(None)

    def finish():
        fw.step()
        now[0] = 300.001

    q.idle = finish
    assert q.wait(tag, timeout=300).ok


def test_completion_during_final_idle_is_consumed():
    wait_through_deadline()


def test_deadline_first_mutant_is_caught():
    original = NodeQueue.poll

    def skip_final(self):
        return [] if self.clock() > 300 else original(self)

    with patch.object(NodeQueue, "poll", skip_final), pytest.raises(TimeoutError):
        wait_through_deadline()
