"""Run text and phases from records with PKGT off, plus a PKGT-only mutant."""

import re
from pathlib import Path
from types import SimpleNamespace

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.application.runtime import card as C
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

ROOT = Path(__file__).resolve().parents[4]
LAYOUT_H = ROOT / "software/firmware/kohakuaccel/include/ka/queue/layout.h"
BASE = 0x10_0000
MACHINE = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
PACKAGE = _rs.package_build('{"credit":32}', "[]", MACHINE, defaults=False)


def offsets() -> dict:
    text = LAYOUT_H.read_text(encoding="utf-8")
    return {
        k.lower(): int(v, 0)
        for k, v in re.findall(r"\bKA_QR_(\w+)\s*=\s*(\d+)\s*,", text)
    }


class Interp:
    def __init__(self, fw_print) -> None:
        self.print, self.n = fw_print, 0

    def run(self, pkg, binds):
        self.n += 1
        self.print(f"PKGS 0 0 {self.n}\n")
        return type("R", (), {"status": 0, "sent": 1, "time": {"head": self.n}})


def node_card():
    mem = MemoryTransport()
    rec = {"bytes": L.REC_BYTES, "fields": offsets()}
    fw = SpecFirmware(mem, BASE, record=rec)
    fw.interp = Interp(fw.print)
    schema = {
        "version": 1,
        "bytes": L.REC_BYTES,
        "fields": {k: {"offset": o, "width": 8} for k, o in rec["fields"].items()},
    }
    q = NodeQueue(mem, BASE, idle=lambda: None, record=L.Record.from_schema(schema))
    q.track_ends = True
    q.init()
    fw.boot()
    c = object.__new__(C.NodeCard)
    c.machine_json = MACHINE
    c.queues = {0: q}
    c._order, c._stdout, c._segs = {}, {}, {}
    return c, q, fw


def run_three(c, q, fw) -> tuple[list, list]:
    tags = [c._submit(0, PACKAGE, [k]) for k in range(3)]
    for _ in tags:
        fw.step()
    done = {t: q.wait(t) for t in tags}
    texts = {t: c._segment(0, t) for t in (tags[2], tags[0], tags[1])}
    return [texts[t] for t in tags], [done[t] for t in tags]


def test_each_run_gets_its_own_text_and_phases_from_its_record():
    c, q, fw = node_card()
    texts, done = run_three(c, q, fw)
    for k, text in enumerate(texts):
        assert text == f"run {len(PACKAGE)}B [{k}]\nPKGS 0 0 {k + 1}\n"
    got = [
        c._result(t, d, SimpleNamespace(steps=[]))
        for t, d in zip(texts, done, strict=True)
    ]
    assert [r["phases"]["head"] for r in got] == [1, 2, 3]
    assert [r["steps"][0][2] for r in got] == [1, 2, 3]


def test_splitting_at_pkgt_lines_gives_nothing_with_the_text_off(monkeypatch):
    monkeypatch.setattr(C.NodeCard, "_segment", C.NodeCard._segment_pkgt)
    c, q, fw = node_card()
    texts, _ = run_three(c, q, fw)
    assert texts != [f"run {len(PACKAGE)}B [{k}]\nPKGS 0 0 {k + 1}\n" for k in range(3)]
