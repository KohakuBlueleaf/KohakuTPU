"""Read and classify the final firmware state before a boot deadline expires."""

from pathlib import Path

import pytest

from kohakuaccel.driver.node.boot.args import ERR_QUEUE, EXIT_BOOTARGS
from kohakuaccel.driver.node.queue import BootRefused, NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.transport.memory import MemoryTransport

LAYOUT = L.Layout.from_machine(
    Path(__file__).resolve().parents[3] / "machines/multimesh_v9t6.json"
)


def test_refusal_published_on_deadline_is_not_a_timeout():
    ticks = [0.0]
    mem = MemoryTransport()
    q = NodeQueue(mem, 0x100000, clock=lambda: ticks[0], layout=LAYOUT)
    q.init()

    def idle():
        ticks[0] = 0.02
        mem.write_block(
            q.base + LAYOUT.fw,
            LAYOUT.line_bytes(LAYOUT.st_fatal, 0, 0, EXIT_BOOTARGS | ERR_QUEUE),
        )

    q._idle = idle
    with pytest.raises(BootRefused, match="queue"):
        q.wait_ready(timeout=0.01)


def test_deadline_first_mutant_is_killed(monkeypatch):
    def deadline_first(self, timeout=120.0):
        start = self._start()
        while self._waited(start, "ready") < timeout:
            got = self._check_ready_state(self.state())
            if got is not None:
                return got
            self._idle()
        raise TimeoutError("firmware never reported ready")

    monkeypatch.setattr(NodeQueue, "wait_ready", deadline_first)
    with pytest.raises(TimeoutError):
        test_refusal_published_on_deadline_is_not_a_timeout()
