"""`NodeCard.stop` (and so `close`) is bounded: a node that never completes
STOP is restarted and asked again, and one that still does not is halted at
its core; the call returns within its bounds, never waits on. The queue
runs on a fake clock that each idle advances by one tick, so the bounds are
checked in the clock's own units, whatever the machine's load."""

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.application.runtime import card as C

BASE = 0x10_0000
BOUND = 0.2
TICK = 0.01


class Win:
    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


class Loader:
    """A node's boot path; `restart` boots the firmware double again when
    `revives`, else leaves the node dead."""

    def __init__(self, fw: SpecFirmware, revives: bool) -> None:
        self.win, self.fw, self.revives = Win(), fw, revives
        self.restarts = 0

    def restart(self, image, args) -> None:
        self.restarts += 1
        if self.revives:
            self.fw.boot()


def card(revives: bool):
    """A card of one node whose firmware reported ready and then hangs; the
    queue steps the firmware only after a restart that revives it. Returns
    the card, its loader and its clock (a one-element list, seconds)."""
    mem = MemoryTransport()
    fw = SpecFirmware(mem, BASE)
    loader = Loader(fw, revives)
    now = [0.0]

    def idle() -> None:
        now[0] += TICK
        if loader.restarts:
            fw.step()

    q = NodeQueue(mem, BASE, idle=idle, timeout=1.0, clock=lambda: now[0])
    q.init()
    fw.boot()
    fw.step()
    c = object.__new__(C.NodeCard)
    c.queues, c.boots, c.nodes, c.fw = {0: q}, {0: (loader, None)}, (0,), None
    c.machine_json = (C.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    c.residents = {0: None}
    c._order, c._stdout, c._segs = {}, {}, {}
    c.stop_timeout = BOUND
    return c, loader, now


def elapsed(c, now) -> float:
    """`c.stop()`'s span on the queue's clock."""
    t0 = now[0]
    c.stop()
    return now[0] - t0


def test_a_hung_node_is_restarted_and_then_stops():
    c, loader, now = card(revives=True)
    # STOP's bound, then the restart's ready wait; the revived node's STOP
    # completes at once.
    assert elapsed(c, now) <= 2 * (BOUND + TICK) + TICK
    # The restart's halt only: the revived node completed STOP.
    assert loader.restarts == 1 and loader.win.stops == 1


def test_a_node_that_never_comes_back_is_halted_within_the_bound():
    c, loader, now = card(revives=False)
    # STOP's bound, then the restart's ready wait, which fails at its bound.
    assert elapsed(c, now) <= 2 * (BOUND + TICK) + TICK
    assert loader.restarts == 1 and loader.win.stops >= 2
