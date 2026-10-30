"""Queue entry read-ahead and ordering (L0.NQ.053-054), with a serial mutant."""

from pathlib import Path

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import PACKAGE_LINE
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport

BASE = 0x10_0000
BAD = 0x10
LAYOUT = L.Layout.from_machine(
    Path(__file__).resolve().parents[3] / "machines/multimesh_v9t6.json"
)


class Interp:
    def run(self, pkg, binds):
        return type("R", (), {"status": pkg[0], "sent": len(pkg)})


def pkg(status=0, fill=0) -> bytes:
    return bytes([status]) + bytes([fill]) * (PACKAGE_LINE - 1)


def node(ahead=True):
    mem = MemoryTransport()
    fw = SpecFirmware(mem, BASE, Interp(), ahead=ahead, layout=LAYOUT)
    q = NodeQueue(mem, BASE, idle=lambda: None, layout=LAYOUT)
    q.init()
    fw.boot()
    return fw, q


def serve(fw, q, n):
    for _ in range(n):
        fw.step()
    return q.poll()


def read_ahead(events) -> bool:
    at = {e: k for k, e in enumerate(events)}
    starts = [e for e in events if e[0] == "start"]
    for _, i, _ in starts[1:]:
        done = next(
            (k for k, e in enumerate(events) if e[:2] == ("complete", i - 1)), None
        )
        got = at.get(("ahead", i, i - 1))
        if got is None or done is None or got > done or ("start", i, True) not in at:
            return False
    return True


def test_entries_are_read_ahead_and_complete_in_order():
    fw, q = node()
    tags = [q.submit(pkg(fill=k), [0x40_0000 + k]) for k in range(4)]
    got = serve(fw, q, 4)
    assert [c.tag for c in got] == tags and all(c.ok for c in got)
    assert [c.value for c in got] == [PACKAGE_LINE] * 4
    assert read_ahead(fw.events)
    assert [e[1] for e in fw.events if e[0] == "complete"] == [0, 1, 2, 3]


def test_the_serial_double_reads_nothing_ahead_and_fails_the_witness():
    fw, q = node(ahead=False)
    for k in range(3):
        q.submit(pkg(fill=k))
    assert len(serve(fw, q, 3)) == 3
    assert not any(e[0] == "ahead" for e in fw.events)
    assert not read_ahead(fw.events)


def test_a_bad_package_fails_in_its_own_turn_with_its_own_status():
    statuses = []
    for ahead in (True, False):
        fw, q = node(ahead)
        tags = [q.submit(pkg()), q.submit(pkg(BAD)), q.submit(pkg())]
        got = serve(fw, q, 3)
        assert [c.tag for c in got] == tags
        statuses.append([c.status for c in got])
    assert statuses[0] == statuses[1] == [0, BAD, 0]


def test_a_failing_run_reads_nothing_ahead():
    fw, q = node()
    q.submit(pkg(BAD))
    q.submit(pkg())
    serve(fw, q, 2)
    assert ("ahead", 1, 0) not in fw.events and ("start", 1, False) in fw.events


def test_stop_and_nop_read_ahead_still_run_in_their_turn():
    fw, q = node()
    q.submit(pkg())
    nop = q.submit(None, op=LAYOUT.op_nop)
    q.submit(pkg())
    stop = q.submit(None, op=LAYOUT.op_stop)
    got = serve(fw, q, 4)
    assert [c.tag for c in got][1::2] == [nop, stop] and all(c.ok for c in got)
    assert [e[1:] for e in fw.events if e[0] == "ahead"] == [(1, 0), (3, 2)]
    assert ("start", 2, False) in fw.events


def test_only_the_entry_after_the_running_one_is_read_ahead():
    fw, q = node()
    q.submit(pkg())
    fw.step()
    q.submit(pkg())
    serve(fw, q, 1)
    assert ("start", 1, False) in fw.events
    assert not any(e[0] == "ahead" for e in fw.events)
