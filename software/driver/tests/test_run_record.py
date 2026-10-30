"""Run records: firmware header versus schema, offset and ring-skew mutants."""

import json
import re
from pathlib import Path

import pytest

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import PACKAGE_LINE, RecordSkew
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport

ROOT = Path(__file__).resolve().parents[3]
LAYOUT_H = ROOT / "software/firmware/kohakuaccel/include/ka/queue/layout.h"
MACHINE = ROOT / "machines" / "multimesh_v9t6.json"
BASE = 0x10_0000
TIMES = ("head", "copy", "bind", "steps", "barrier", "local", "total", "first", "last")


def header() -> dict:
    text = LAYOUT_H.read_text(encoding="utf-8")
    pairs = re.findall(r"#define\s+(\w+)\s+(0x[0-9A-Fa-f]+|\d+)", text)
    pairs += re.findall(r"\b(KA_\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*,", text)
    return {k: int(v, 0) for k, v in pairs}


def firmware_record() -> dict:
    h = header()
    fields = {
        k.removeprefix("KA_QR_").lower(): v
        for k, v in h.items()
        if k.startswith("KA_QR_")
    }
    return {"bytes": h["KA_Q_REC_BYTES"], "fields": fields}


def schema_record() -> dict:
    return json.loads(MACHINE.read_text(encoding="utf-8"))["machine"]["fw"]["queue"][
        "record"
    ]


class Interp:
    def __init__(self) -> None:
        self.runs = 0

    def run(self, pkg, binds):
        self.runs += 1
        time = {k: 10 * self.runs + i for i, k in enumerate(TIMES)}
        return type("R", (), {"status": 0, "sent": len(pkg), "time": time})


def records(schema: dict, n: int = 3, double=SpecFirmware) -> list:
    mem = MemoryTransport()
    fw = double(mem, BASE, Interp(), record=firmware_record())
    q = NodeQueue(mem, BASE, idle=fw.step, record=L.Record.from_schema(schema))
    q.init()
    fw.boot()
    tags = [q.submit(bytes(PACKAGE_LINE)) for _ in range(n)]
    return [q.wait(t).record for t in tags]


def expected(n: int = 3) -> list:
    line = len(f"run {PACKAGE_LINE}B []\n")
    return [
        {
            "so_wr": line * (r + 1),
            **{k: 10 * (r + 1) + i for i, k in enumerate(TIMES)},
            "list_index": 0,
            "item_tag": 0,
            "epoch": 0,
        }
        for r in range(n)
    ]


def strip(got: list) -> list:
    return [
        {k: v for k, v in r.items() if k not in ("tag", "sent", "idle")} for r in got
    ]


def test_the_header_and_the_schema_give_the_same_record():
    h, s = header(), schema_record()
    assert s["bytes"] == h["KA_Q_REC_BYTES"] == L.REC_BYTES
    assert s["version"] == h["KA_Q_REC_VERSION"]
    assert {k: f["offset"] for k, f in s["fields"].items()} == firmware_record()[
        "fields"
    ]
    assert {f["width"] for f in s["fields"].values()} == {h["KA_Q_REC_WORD"]}
    assert h["KA_Q_VERSION"] == L.VERSION


def test_records_decode_with_the_schema_as_the_firmware_writes_them():
    got = records(schema_record())
    assert strip(got) == expected()
    assert [r["tag"] for r in got] == [1, 2, 3]


def test_a_wrong_schema_offset_is_caught():
    bad = json.loads(json.dumps(schema_record()))
    f = bad["fields"]
    f["head"]["offset"], f["copy"]["offset"] = f["copy"]["offset"], f["head"]["offset"]
    assert strip(records(bad)) != expected()


def test_a_record_ring_out_of_step_with_the_cq_raises():
    class Skewed(SpecFirmware):
        def write_record(self, slot, tag, time):
            super().write_record((slot + 1) % self.cq_n, tag, time)

    with pytest.raises(RecordSkew):
        records(schema_record(), double=Skewed)


def test_a_version_3_region_has_no_record_ring():
    q = NodeQueue(MemoryTransport(), BASE)
    v4 = dict(q.geo)
    q.speak(3)
    assert q.geo["end"] == v4["rec"] and q.bind_off < -(-v4["end"] // L.LINE) * L.LINE
    with pytest.raises(ValueError):
        q.speak(3, L.Record.from_schema(schema_record()))
    with pytest.raises(ValueError):
        q.speak(5)
