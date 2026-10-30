"""Prepared replay preserves frozen entries, queue epochs and bounded ownership."""

from unittest.mock import patch

import pytest

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as QL
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.application.runtime.card import NodeCard, PreparedEvents
from kohakutpu.application.runtime.device import (
    PREPARED_REPLAY_ENV,
    REPLAY_PACKAGE_BYTES_ENV,
    Device,
    Recorded,
)
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

MACHINE = (MF.MACHINES / "vlt_v9t6s_1n.json").read_text(encoding="utf-8")
LAYOUT = QL.Layout.from_machine(MACHINE)
PACKAGE = _rs.package_build('{"credit": 32}', "[]", MACHINE, defaults=False)


def device():
    dev = Device(kind="offline", board="vlt_v9t6s_1n")
    q = NodeQueue(
        MemoryTransport(),
        0x100000,
        spill=lambda n: dev.memory.alloc(n, owner="package"),
        unspill=dev.memory.free,
        layout=LAYOUT,
    )
    q.init()
    fw = SpecFirmware(q.mem, q.base, layout=LAYOUT)
    fw.boot()
    card = object.__new__(NodeCard)
    card.queues = {0: q}
    card.machine_json = MACHINE
    card.queue_layout = LAYOUT
    card.nodes = (0,)
    card.runs = 0
    card.debug = False
    card._order, card._stdout, card._segs = {}, {}, {}
    card.residents = {}
    card._timeout = 1
    card.q = q
    card.memory = dev.memory
    card.close = dict
    card.drop_spilled = q.drop_spilled
    dev.card = card
    q.idle = fw.step
    return dev, q


def recording(dev, raw=PACKAGE):
    return Recorded(
        [("run", {0: (raw, ())})],
        [],
        dev,
        [("prepared", "dsl", {}, [], [], (0,))],
    )


def test_prepared_replay_reuses_bytes_and_wrapper_and_closes_after_device():
    dev, q = device()
    dev.prepared_replay = True
    log = recording(dev)
    dev.prepare_recorded(log)
    pkg = log.prepared[0][1][0][1]
    for _ in range(3):
        pending = dev.replay(log, wait=False)[0]
        assert pending.pending.tags[0][1] is pkg
        dev.collect(pending)
        assert pkg.to_bytes() == PACKAGE
    assert q.counters["uploads"] == 1 and q.prepared_bytes
    dev.close()
    assert not q._package_pins and not q.prepared_bytes
    log.close()


def test_changed_binding_and_package_refuse_before_publication():
    for replacement in ((PACKAGE, (1234,)), (PACKAGE + bytes(32), ())):
        dev, q = device()
        dev.prepared_replay = True
        log = recording(dev)
        dev.prepare_recorded(log)
        log[0][1][0] = replacement
        with pytest.raises(ValueError, match="changed"):
            dev.replay(log, wait=False)
        assert q.counters["submitted"] == 0
        log.close()
        dev.close()


def test_shallow_snapshot_mutant_is_caught(monkeypatch):
    monkeypatch.setattr(PreparedEvents, "snapshot", staticmethod(tuple))
    with pytest.raises(pytest.fail.Exception):
        test_changed_binding_and_package_refuse_before_publication()


def test_device_wide_budget_includes_other_logs_and_closed_inflight_packages():
    dev, q = device()
    dev.prepared_replay = True
    n = (len(PACKAGE) + 63) // 64 * 64
    dev.replay_package_bytes = dev.memory.dram.align
    first, second = recording(dev), recording(dev, PACKAGE + bytes(32))
    dev.prepare_recorded(first)
    assert first.prepared.selection["pinned_packages"] == 1
    dev.prepare_recorded(second)
    assert second.prepared.selection["pinned_packages"] == 0
    assert second.prepared.selection["streamed_calls"] == 1
    second.prepared.close()
    second.prepared = None
    pending = dev.replay(first, wait=False)[0]
    first.close()
    assert q.prepared_bytes == n
    dev.prepare_recorded(second)
    assert second.prepared.selection["pinned_packages"] == 0
    second.prepared.close()
    second.prepared = None
    dev.collect(pending)
    assert q.prepared_bytes == 0
    dev.prepare_recorded(second)
    assert second.prepared.selection["pinned_packages"] == 1
    second.close()
    dev.close()


def test_budget_mutant_is_caught(monkeypatch):
    monkeypatch.setattr(NodeQueue, "prepared_bytes", property(lambda self: 0))
    monkeypatch.setattr(NodeQueue, "prepared_addresses", lambda self: set())
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        test_device_wide_budget_includes_other_logs_and_closed_inflight_packages()


def test_partial_prepare_failure_rolls_back_uploads():
    dev, q = device()
    dev.prepared_replay = True
    log = recording(dev)
    log.append(("run", {0: (PACKAGE + bytes(32), ())}))
    original = q.prepare
    calls = [0]

    def fail(raw):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("second upload")
        return original(raw)

    with patch.object(q, "prepare", fail), pytest.raises(OSError):
        dev.prepare_recorded(log)
    assert not q._package_pins and not q._cache and q.prepared_bytes == 0
    log.close()
    dev.close()


@pytest.mark.parametrize(
    "env,value",
    [
        (PREPARED_REPLAY_ENV, "bad"),
        (REPLAY_PACKAGE_BYTES_ENV, "bad"),
        (REPLAY_PACKAGE_BYTES_ENV, "-1"),
    ],
)
def test_invalid_preparation_config_does_not_open_backend(env, value, monkeypatch):
    monkeypatch.setenv(env, value)
    with (
        patch("kohakutpu.application.runtime.device.OfflineCard") as opened,
        pytest.raises(ValueError),
    ):
        Device(kind="offline", board="vlt_v9t6s_1n")
    opened.assert_not_called()
