"""The daemon's node ops against the spec firmware double: one round trip each.

A Daemon over an in-memory card holds the node queues; `poll_idle` steps
`SpecFirmware` (the firmware half of the protocol, written from the spec)
between two polls, the way the Verilator backend advances its model. A client
drives packages and node heaps through `RemoteNodeQueue`.
"""

import threading

import pytest
from kohakuaccel.compiler.package.build import PackageBuilder
from kohakuaccel.compiler.package.format import Package
from kohakuaccel.driver.daemon.client import (
    DaemonClient,
    DaemonError,
    RemoteNodeQueue,
)
from kohakuaccel.driver.daemon.server import Daemon
from kohakuaccel.driver.node.queue import NodeError
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport

BASE, BASE1 = 0x10_0000, 0x50_0000


@pytest.fixture
def card():
    mem = MemoryTransport()
    fws: dict[int, SpecFirmware] = {}
    polls = {"n": 0}

    def step():
        polls["n"] += 1
        for fw in fws.values():
            fw.step()

    d = Daemon(mem, node_mem=mem, poll_idle=step, port=0)
    port = d.start()
    yield d, port, mem, fws, polls
    d.stop()


def _attach(port, fws, mem, node=0, base=BASE, **geo):
    q = RemoteNodeQueue(DaemonClient(port=port), node=node, base=base, **geo)
    fw = SpecFirmware(mem, base)
    fw.boot()
    fws[node] = fw
    return q


def test_package_runs_in_one_round_trip(card):
    _, port, mem, fws, polls = card
    q = _attach(port, fws, mem)
    assert q.wait_ready()["state"] == "ready"
    calls0 = q.client._id
    c = q.run(PackageBuilder().build().to_bytes(), bindings=[0x40_0000, 0x50_0000])
    assert c.ok and q.client._id == calls0 + 1
    assert polls["n"] >= 1  # the daemon polled; the client did not
    assert "run 96B [4194304, 5242880]" in q.read_stdout()
    assert q.counters["uploads"] == 1


def test_submit_then_wait_and_a_reused_package(card):
    _, port, mem, fws, _ = card
    q = _attach(port, fws, mem)
    pkg = Package(payloads=[1, 2, 3]).to_bytes()
    tags = [q.submit(pkg), q.submit(pkg, bindings=[7]), q.submit(None, qop=L.OP_NOP)]
    assert [q.wait(t).ok for t in reversed(tags)] == [True] * 3
    assert q.counters["cache_hits"] == 1 and q.counters["uploads"] == 1


def test_a_failed_entry_raises_node_error_with_its_status(card):
    _, port, mem, fws, _ = card
    q = _attach(port, fws, mem)
    tag = q.submit(None, qop=0x77)
    with pytest.raises(NodeError, match="BAD_OP"):
        q.wait(tag)
    assert q.wait(q.submit(None, qop=0x77), check=False).status == 0x40


def test_node_heap_ops(card):
    _, port, mem, fws, _ = card
    q = _attach(port, fws, mem)
    q.heap(L.MEM_DRAM, 0x80_0000, 1 << 16)
    a = q.alloc(L.MEM_DRAM, 100, align=256, tag=5)
    b = q.alloc(L.MEM_DRAM, 1)
    assert (a, b) == (0x80_0000, 0x80_0080)
    assert q.heap_stats(L.MEM_DRAM)["live"] == 2
    with pytest.raises(NodeError, match="NO_MEMORY"):
        q.alloc(L.MEM_DRAM, 1 << 17)
    q.free(L.MEM_DRAM, a)
    with pytest.raises(NodeError, match="BAD_FREE"):
        q.free(L.MEM_DRAM, a)
    q.free(L.MEM_DRAM, b)
    st = q.heap_stats(L.MEM_DRAM)
    assert st["free"] == st["largest"] == 1 << 16 and st["blocks"] == 1


def test_two_nodes_two_clients_interleave(card):
    """Two clients wait on two nodes at once; the daemon polls both between
    other ops, and each gets its own completions."""
    _, port, mem, fws, _ = card
    qa = _attach(port, fws, mem, node=0, base=BASE)
    qb = _attach(port, fws, mem, node=1, base=BASE1)
    errs = []

    def work(q, k):
        try:
            for rep in range(6):
                c = q.run(Package(payloads=[k, rep]).to_bytes(), bindings=[k])
                if not c.ok:
                    errs.append((k, rep, c))
        except Exception as exc:
            errs.append(repr(exc))

    ta = threading.Thread(target=work, args=(qa, 1))
    tb = threading.Thread(target=work, args=(qb, 2))
    ta.start(), tb.start()
    ta.join(30), tb.join(30)
    assert errs == []
    assert qa.counters["submitted"] == qb.counters["submitted"] == 6


def test_stop_and_unknown_node(card):
    _, port, mem, fws, _ = card
    q = _attach(port, fws, mem)
    assert q.stop(5).ok
    with pytest.raises(DaemonError, match="no attached queue"):
        DaemonClient(port=port).call("node_state", node=3)
    with pytest.raises(DaemonError, match="node loader"):
        DaemonClient(port=port).call("node_load", node=0, elf="", args={}, mode="burn")


def test_a_wait_times_out_without_wedging_the_daemon(card):
    _, port, mem, fws, _ = card
    q = _attach(port, fws, mem)
    tag = q.submit(None, qop=L.OP_NOP)
    fws.pop(0)  # the firmware stops answering
    with pytest.raises(DaemonError, match="TimeoutError"):
        q.client.call("node_wait", node=0, tag=tag, timeout=0.3)
    other = DaemonClient(port=port)
    other.call("write64", addr=0x100, data=0x1234)
    assert other.call("read64", addr=0x100) == 0x1234
