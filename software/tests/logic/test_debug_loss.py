"""Trace-loss refusal is acknowledged per region, never a complete report."""

import struct
from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakuaccel.driver.transport.base import Transport
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.card.common import DBG_H_RD
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.stack.rs import _rs


class Region(Transport):
    def __init__(self, data):
        self.data = data

    def read64(self, addr):
        return int.from_bytes(self.read_block(addr, 8), "little")

    def write64(self, addr, data):
        self.write_block(addr, data.to_bytes(8, "little"))

    def read_block(self, addr, nbytes):
        return bytes(self.data[addr : addr + nbytes])

    def write_block(self, addr, data):
        self.data[addr : addr + len(data)] = data


def test_trace_loss_refuses_report_but_acknowledged_loss_does_not_poison_next_run(
    monkeypatch,
):
    region = bytearray(192)

    def header(wr, lost, rd, tag):
        struct.pack_into(
            "<9Q", region, 0, 0x31474244414B, 1, 0, 0, 128, 64, wr, lost, rd
        )
        struct.pack_into("<8Q", region, 128, 1, tag, 0, 12, 0, 0, 0, 0)

    dev = D.Device("offline", board="multimesh_v9t6", queue_depth=1)
    try:
        machine = generated.rs.machine_json(dev.machine)
        card = object.__new__(C.NodeCard)
        card.debug = True
        card.nodes = (0,)
        card.debug_regions = {0: (0, len(region))}
        card.machine_json = machine
        card.mem = UnitGlobal(Region(region), [0], [0], len(region))
        card.queue_layout = L.Layout.from_machine(machine)
        header(64, 64, 0, 7)
        with pytest.raises(_rs.Refused, match="DBG.TRACE.002"):
            card.debug_report()
        assert struct.unpack_from("<Q", region, DBG_H_RD)[0] == 64
        header(128, 64, 64, 8)
        report = card.debug_report()
        assert report["packages"][0]["tag"] == 8
        assert report["packages"][0]["steps"][0]["cycle"] == 12

        # Load publishes a fresh header even when the address is reused.
        monkeypatch.setattr(
            C.abi,
            "check",
            lambda *args, **kw: {
                "package_features": 1,
                "debug": True,
                "queue": card.queue_layout.version,
                "last": 0,
            },
        )
        q = NodeQueue(HostMemory(), 0x1000, layout=card.queue_layout)
        loader = SimpleNamespace(
            win=SimpleNamespace(stop=lambda: None),
            load=lambda *args: header(128, 64, 64, 9),
        )
        card.queues = {0: q}
        card.boots = {0: (loader, SimpleNamespace(regions=[]))}
        card._place = "axi"
        monkeypatch.setattr(card, "_wait_ready", lambda node: None)
        card._order, card._stdout, card._segs = {}, {}, {}
        C.NodeCard.load(card, fw="debug.elf", debug=True)
        with pytest.raises(_rs.Refused, match="DBG.TRACE.002"):
            card.debug_report()
        assert struct.unpack_from("<Q", region, DBG_H_RD)[0] == 128
    finally:
        dev.close()
