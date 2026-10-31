"""A prepared graph is one execution list; its pins outlive the terminal completion."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakutpu.application.runtime.graph.firmware import FirmwareRunner
from kohakutpu.application.runtime.offline import HostMemory

MACHINE = Path(__file__).resolve().parents[4] / "machines" / "multimesh_v9t6.json"
LAYOUT = L.Layout.from_machine(MACHINE)


def queue():
    mem = HostMemory()
    allocated = []

    def spill(size):
        at = 0x800000 + sum(allocated)
        allocated.append(size)
        return at

    q = NodeQueue(
        mem,
        0,
        sq_n=4,
        cq_n=4,
        spill=spill,
        unspill=lambda at: None,
        record=LAYOUT.record,
        layout=LAYOUT,
    )
    q.init()
    firmware = SpecFirmware(mem, 0, record=LAYOUT.record, layout=LAYOUT)
    firmware.boot()
    q.idle = firmware.step
    return q, firmware


def test_a_graph_runs_its_packets_in_order_as_one_submission():
    q, firmware = queue()
    prepared = FirmwareRunner(q).prepare((bytes(64), bytes([1]) * 64), ())
    completion = prepared.submit()
    assert completion.record["list_index"] == 2
    assert [(e[1], e[2]) for e in firmware.events if e[0] == "list-item"] == [
        (0, 1),
        (1, 2),
    ]
    assert q.counters["submitted"] == 1
    prepared.close()
    assert not q._package_pins


def test_a_failed_upload_leaves_no_pin(monkeypatch):
    q, _ = queue()
    original = q.prepare
    calls = 0

    def fail_second(raw):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("upload")
        return original(raw)

    with monkeypatch.context() as local:
        local.setattr(q, "prepare", fail_second)
        with pytest.raises(OSError, match="upload"):
            FirmwareRunner(q).prepare((bytes(64), bytes([1]) * 64), ())
    assert not q._package_pins


def test_an_ambiguous_wait_keeps_the_list_owned(monkeypatch):
    q, _ = queue()
    prepared = FirmwareRunner(q).prepare((bytes(64),), ())
    monkeypatch.setattr(q, "submit_list", Mock(return_value=9))
    monkeypatch.setattr(q, "wait", Mock(side_effect=TimeoutError("unresolved")))
    with pytest.raises(TimeoutError):
        prepared.submit()
    with pytest.raises(RuntimeError, match="unresolved"):
        prepared.submit()
    prepared.close()
    assert q._package_pins and not prepared.execution.closed
    # Only an observed terminal completion permits the final release.
    prepared.busy = False
    prepared.close()
    assert not q._package_pins
