"""Diagnostic recovery revokes queue ownership only after a sysnode reset."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue import FirmwareFatal
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime.offline import HostMemory

MACHINE = Path(__file__).resolve().parents[4] / "machines" / "multimesh_v9t6.json"


def test_diagnostic_restart_waits_for_reset_before_releasing_ownership(monkeypatch):
    layout = L.Layout.from_machine(MACHINE)
    events, freed = [], []
    card = object.__new__(C.SiliconCard)
    card.nodes = (0, 1)
    card.t, card.board = object(), {}
    card.fw = Path("firmware.elf")
    card.machine_json = MACHINE.read_text(encoding="utf-8")
    card._order, card._stdout, card._segs = {}, {}, {}
    card.residents = {}
    card.queues, card.boots = {}, {}
    firmwares = {}
    for node in card.nodes:
        mem = HostMemory()
        q = NodeQueue(
            mem,
            0,
            sq_n=2,
            cq_n=4,
            layout=layout,
            spill=lambda size: 1 << 21,
            unspill=freed.append,
        )
        q.init()
        fw = SpecFirmware(mem, 0, layout=layout)
        fw.boot()
        firmwares[node] = fw

        def restart(image, args, node=node, fw=fw):
            events.append(("boot", node))
            fw.boot()

        loader = SimpleNamespace(
            win=SimpleNamespace(stop=lambda: events.append("halt")),
            restart=restart,
        )
        card.queues[node] = q
        card.boots[node] = (loader, None)
    card.timeout = 1.0
    q = card.queues[1]
    package = q.prepare(b"held package".ljust(32, b"\0"))
    tag = q.submit(package, [0x1000])
    q.flush()
    firmwares[1]._fatal(0xE000000000000014)
    with pytest.raises(FirmwareFatal):
        q.poll()
    package.close()

    locked = False

    def reset():
        assert q._quarantined and not freed
        assert tag in q._package_tags
        events.append("reset")

    wizard = SimpleNamespace(load=reset, locked=lambda: locked)
    monkeypatch.setattr(
        C,
        "CardClocks",
        lambda transport, board: SimpleNamespace(
            mesh=lambda node: wizard, meshes=[wizard]
        ),
    )
    monkeypatch.setattr(
        card, "_wait_ready", lambda node, ready: events.append(("ready", node))
    )
    report = {"packages": []}
    monkeypatch.setattr(card, "debug_report", lambda node: report)

    def run(*args, **kwargs):
        assert not q._quarantined and freed == [package.address]
        assert events[-2:] == [("ready", 0), ("ready", 1)]
        events.append("run")

    monkeypatch.setattr(q, "run", run)
    with pytest.raises(C.ClockMismatch, match="sysnode reset did not complete"):
        card._debug_rerun(1, b"package", None, report)
    assert events == ["reset"]
    assert q._quarantined and not freed and tag in q._package_tags

    locked = True
    card._debug_rerun(1, b"package", None, report)
    assert events == [
        "reset",
        "reset",
        "halt",
        ("boot", 0),
        "halt",
        ("boot", 1),
        ("ready", 0),
        ("ready", 1),
        "run",
    ]
    events.clear()
    card.restart(nodes=(1,))
    assert events == ["halt", ("boot", 1), ("ready", 1)]
