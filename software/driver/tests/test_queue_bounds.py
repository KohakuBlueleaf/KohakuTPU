"""Every wait on a node queue is bounded by the queue's `timeout`: a node
that never completes (a firmware booted but never stepping) makes STOP, a
full SQ and a drain raise `TimeoutError` within the bound, never wait on.
The queue runs on a fake clock that each idle advances by one tick, so the
bounds are checked in the clock's own units, whatever the machine's load."""

import pytest

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport

BASE = 0x10_0000
BOUND = 0.3
TICK = 0.01


def stuck() -> tuple[NodeQueue, list]:
    """A queue whose firmware reported ready and then never runs again, and
    its clock (a one-element list, seconds)."""
    mem = MemoryTransport()
    fw = SpecFirmware(mem, BASE)
    now = [0.0]

    def idle() -> None:
        now[0] += TICK

    q = NodeQueue(mem, BASE, idle=idle, timeout=BOUND, clock=lambda: now[0])
    q.init()
    fw.boot()
    fw.step()
    assert q.wait_ready(1.0)["state"] == "ready"
    return q, now


def within(f, now) -> float:
    """How long `f` ran on the queue's clock before its `TimeoutError`."""
    t0 = now[0]
    with pytest.raises(TimeoutError):
        f()
    return now[0] - t0


def test_stop_on_a_node_that_never_completes_raises_within_the_bound():
    q, now = stuck()
    assert BOUND <= within(q.stop, now) <= BOUND + 2 * TICK


def test_a_call_names_its_own_bound():
    q, now = stuck()
    assert 0.05 <= within(lambda: q.stop(timeout=0.05), now) <= 0.05 + 2 * TICK


def test_a_full_sq_raises_within_the_bound():
    q, now = stuck()
    for _ in range(q.sq_n):
        q.submit(None)
    assert BOUND <= within(lambda: q.submit(None), now) <= BOUND + 2 * TICK


def test_a_drain_raises_within_the_bound():
    q, now = stuck()
    q.submit(None)
    assert BOUND <= within(q._drain_all, now) <= BOUND + 2 * TICK
