"""The daemon against the in-memory card: every feature, no hardware.

The model backend is the point of these tests -- queue discipline,
leases, and the clock governor are all policy, and policy must be
provable with nothing attached. The wizard registers are real enough
that `CardClocks` runs unmodified: dividers live in the memory dict and
the status word intercepts as LOCKED.
"""

import threading
import time

import pytest
from kohakuaccel.daemon.__main__ import WizardClockCtl, model_transport
from kohakuaccel.daemon.client import DaemonClient, DaemonError, DaemonTransport
from kohakuaccel.daemon.server import Daemon
from kohakutpu.clock.card import load_board

BOARD = load_board("multimesh_v65")
LOW = BOARD["profiles"]["low"]
MID = BOARD["profiles"]["mid"]


@pytest.fixture
def daemon():
    transport = model_transport(BOARD)
    d = Daemon(
        transport,
        board=BOARD,
        clock_ctl=WizardClockCtl(transport, BOARD),
        idle_seconds=0.2,
        port=0,
    )
    port = d.start()
    yield d, port, transport
    d.stop()


def _client(port):
    return DaemonClient(port=port)


def test_word_and_block_ops_roundtrip(daemon):
    d, port, transport = daemon
    t = DaemonTransport(client=_client(port))
    t.write64(0x100, 0x1122334455667788)
    assert t.read64(0x100) == 0x1122334455667788
    data = bytes(range(64))
    t.write_block(0x1000, data)
    assert t.read_block(0x1000, 64) == data
    # The daemon-side transport holds the same bytes: one card, one truth.
    assert transport.read_block(0x1000, 64) == data


def test_write32_read32_leave_the_neighbour_alone(daemon):
    d, port, _ = daemon
    t = DaemonTransport(client=_client(port))
    t.write64(0x200, 0xAAAAAAAA_BBBBBBBB)
    t.write32(0x200, 0x11111111)
    t.write32(0x204, 0x22222222)
    assert t.read32(0x200) == 0x11111111
    assert t.read32(0x204) == 0x22222222
    assert t.read64(0x200) == 0x22222222_11111111


def test_two_clients_one_card(daemon):
    """Interleaved clients, disjoint regions, correct bytes at the end:
    the single-worker queue is the mechanism, this is its observable."""
    d, port, _ = daemon
    a = DaemonTransport(client=_client(port))
    b = DaemonTransport(client=_client(port))
    pa = bytes([i & 0xFF for i in range(512)])
    pb = bytes([(255 - i) & 0xFF for i in range(512)])
    errs = []

    def hammer(t, base, pat):
        try:
            for rep in range(8):
                t.write_block(base, pat)
                if t.read_block(base, len(pat)) != pat:
                    errs.append(f"corrupt at {base:#x} rep {rep}")
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))

    ta = threading.Thread(target=hammer, args=(a, 0x10000, pa))
    tb = threading.Thread(target=hammer, args=(b, 0x20000, pb))
    ta.start(), tb.start()
    ta.join(10), tb.join(10)
    assert errs == []


def test_lease_overlap_refused_and_freed_on_disconnect(daemon):
    d, port, _ = daemon
    a, b = _client(port), _client(port)
    lease = a.claim(mesh=0, base=0x1800_0000, size=1 << 24)
    with pytest.raises(DaemonError):
        b.claim(mesh=0, base=0x1880_0000, size=1 << 24)
    b.claim(mesh=1, base=0x1800_0000, size=1 << 24)  # other mesh is free
    a.close()
    deadline = time.time() + 5
    while time.time() < deadline:
        if not any(
            v["mesh"] == 0 for v in b.status()["leases"].values()
        ):
            break
        time.sleep(0.05)
    b.claim(mesh=0, base=0x1800_0000, size=1 << 24)
    assert lease  # the id existed; its span is claimable again


def test_governor_startup_is_idle(daemon):
    d, port, _ = daemon
    c = _client(port)
    clocks = c.clocks()
    for mesh, rates in clocks.items():
        for name, want in LOW.items():
            assert rates[name] == pytest.approx(want), (mesh, name)


def test_governor_scoped_boost_then_idle_drop(daemon):
    d, port, _ = daemon
    c = _client(port)
    token = c.run_begin([1], "mid")
    clocks = c.clocks()
    for name, want in MID.items():
        assert clocks[1][name] == pytest.approx(want)
    for name, want in LOW.items():
        assert clocks[0][name] == pytest.approx(want)  # NOT boosted
    c.run_end(token)
    deadline = time.time() + 5
    while time.time() < deadline:
        if c.status()["governor"]["current"] == {
            str(m): "low" for m in range(BOARD["meshes"])
        } or c.status()["governor"]["current"] == {
            m: "low" for m in range(BOARD["meshes"])
        }:
            break
        time.sleep(0.1)
    for name, want in LOW.items():
        assert c.clocks()[1][name] == pytest.approx(want)


def test_governor_client_death_ends_its_runs(daemon):
    d, port, _ = daemon
    a, b = _client(port), _client(port)
    a.run_begin([2], "mid")
    a.close()
    deadline = time.time() + 5
    while time.time() < deadline:
        if not b.status()["governor"]["runs"]:
            break
        time.sleep(0.05)
    assert b.status()["governor"]["runs"] == {}
    deadline = time.time() + 5
    while time.time() < deadline:
        rates = b.clocks()[2]
        if all(rates[n] == pytest.approx(w) for n, w in LOW.items()):
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"mesh 2 never returned to low: {b.clocks()[2]}")
