"""A failed wait plus firmware quiesce completes before the host deadline."""

from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.boot.args import bound
from kohakuaccel.driver.node.queue import NodeError, NodeQueue
from kohakuaccel.driver.node.queue import layout as QL
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.application.runtime import card

LAYOUT = QL.Layout.from_machine(card.MACHINES / "multimesh_v9t6.json")


def failed_run():
    host_seconds, hz = 30.0, 100_000_000
    wait_cycles = bound(card.firmware_wait_seconds(host_seconds, None), hz / 1e6)
    # engine.c wait_until + ka_engine_quiesce each own one timeout; one second
    # represents finite setup/report work, not an extra firmware wait.
    completion_at = 2 * wait_cycles + hz
    cycles = [0]
    mem = MemoryTransport()
    q = NodeQueue(mem, 0x100000, clock=lambda: cycles[0] / hz, layout=LAYOUT)
    q.init()
    fw = SpecFirmware(
        mem,
        q.base,
        SimpleNamespace(run=lambda *args: SimpleNamespace(status=0x30, sent=0)),
        layout=LAYOUT,
    )
    fw.boot()

    def idle():
        cycles[0] += hz // 2
        if cycles[0] >= completion_at:
            fw.step()

    q.idle = idle
    with pytest.raises(NodeError, match="TIMEOUT"):
        q.wait(q.submit(b"x" * 32), timeout=host_seconds)
    return cycles[0] / hz


def test_failure_and_quiesce_fit_host_budget():
    assert failed_run() == 21.0
    assert card.firmware_wait_seconds(30.0, 0.02) == 0.02


def test_half_budget_mutant(monkeypatch):
    monkeypatch.setattr(
        card,
        "firmware_wait_seconds",
        lambda timeout, requested: timeout / 2 if requested is None else requested,
    )
    with pytest.raises(TimeoutError):
        failed_run()
