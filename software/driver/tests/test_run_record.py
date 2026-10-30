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
from kohakutpu.compiler import _rs

ROOT = Path(__file__).resolve().parents[3]
LAYOUT_H = ROOT / "software/firmware/kohakuaccel/include/ka/queue/layout.h"
STATUS_H = ROOT / "software/firmware/kohakuaccel/include/ka/package/status.h"
MACHINE = ROOT / "machines" / "multimesh_v9t6.json"
BASE = 0x10_0000
TIMES = (
    "head",
    "copy",
    "bind",
    "steps",
    "barrier",
    "local",
    "total",
    "first",
    "last",
    "sent",
    "idle",
)


def header(path: Path = LAYOUT_H) -> dict:
    text = path.read_text(encoding="utf-8")
    pairs = re.findall(r"#define\s+(\w+)\s+(0x[0-9A-Fa-f]+|\d+)[uUlL]*", text)
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


def machine_queue() -> dict:
    return json.loads(MACHINE.read_text(encoding="utf-8"))["machine"]["fw"]["queue"]


def schema_record() -> dict:
    return machine_queue()["record"]


LAYOUT = L.Layout.from_schema(machine_queue())


class Interp:
    def __init__(self) -> None:
        self.runs = 0

    def run(self, pkg, binds):
        self.runs += 1
        time = {k: 10 * self.runs + i for i, k in enumerate(TIMES)}
        return type("R", (), {"status": 0, "sent": len(pkg), "time": time})


def records(schema: dict, n: int = 3, double=SpecFirmware) -> list:
    mem = MemoryTransport()
    fw = double(mem, BASE, Interp(), record=firmware_record(), layout=LAYOUT)
    q = NodeQueue(
        mem, BASE, idle=fw.step, record=L.Record.from_schema(schema), layout=LAYOUT
    )
    q.init()
    fw.boot()
    tags = [q.submit(bytes(PACKAGE_LINE)) for _ in range(n)]
    return [q.wait(t).record for t in tags]


def expected(n: int = 3, epoch: int = 0) -> list:
    line = len(f"run {PACKAGE_LINE}B []\n")
    return [
        {
            "tag": r + 1,
            "so_wr": line * (r + 1),
            **{k: 10 * (r + 1) + i for i, k in enumerate(TIMES)},
            "list_index": 0,
            "item_tag": 0,
            "epoch": epoch,
        }
        for r in range(n)
    ]


def assert_layout_matches(h: dict, st: dict, q: dict, py_layout: L.Layout, rs_layout):
    assert q["magic"] == h["KA_Q_MAGIC"] == py_layout.magic == rs_layout.magic
    assert (
        q["min_version"]
        == h["KA_Q_MIN_VERSION"]
        == py_layout.min_version
        == rs_layout.min_version
    )
    assert q["version"] == h["KA_Q_VERSION"] == py_layout.version == rs_layout.version
    assert py_layout.versions == tuple(
        range(h["KA_Q_MIN_VERSION"], h["KA_Q_VERSION"] + 1)
    )
    assert (
        q["list_version"]
        == h["KA_Q_LIST_VERSION"]
        == py_layout.list_version
        == rs_layout.list_version
    )
    assert q["line"] == h["KA_Q_LINE"] == py_layout.line == rs_layout.line
    assert (
        q["sq_bytes"] == h["KA_Q_SQ_BYTES"] == py_layout.sq_bytes == rs_layout.sq_bytes
    )
    assert (
        q["cq_bytes"] == h["KA_Q_CQ_BYTES"] == py_layout.cq_bytes == rs_layout.cq_bytes
    )
    assert q["sq_words"] == h["KA_SQ_WORDS"] == py_layout.sq_words == rs_layout.sq_words
    assert py_layout.sq_tail_words == h["KA_SQ_WORDS"] - 2
    assert (
        q["nbind_shift"]
        == h["KA_SQ_NBIND_SHIFT"]
        == py_layout.nbind_shift
        == rs_layout.nbind_shift
    )
    assert (
        q["stdio_mask"]
        == h["KA_QI_STDIO_MASK"]
        == py_layout.stdio_mask
        == rs_layout.stdio_mask
    )
    assert (
        py_layout.tail_poll_bytes
        == h["KA_Q_SO_WR"] + h["KA_Q_LINE"] - h["KA_Q_CQ_TAIL"]
    )
    assert py_layout.so_wr_from_cq_tail == h["KA_Q_SO_WR"] - h["KA_Q_CQ_TAIL"]
    assert q["op_mask"] == h["KA_SQ_OP_MASK"] == py_layout.op_mask == rs_layout.op_mask
    assert (
        q["cq_status_mask"]
        == h["KA_CQ_STATUS_MASK"]
        == py_layout.cq_status_mask
        == rs_layout.cq_status_mask
    )
    for name in (
        "info",
        "epoch",
        "fw",
        "list_abi",
        "sq_tail",
        "sq_head",
        "cq_tail",
        "cq_head",
        "so_wr",
        "so_rd",
        "si_wr",
        "si_rd",
        "units",
        "heaps",
        "sq",
    ):
        c_val = h[f"KA_Q_{name.upper()}"]
        assert (
            q["offsets"][name]
            == c_val
            == getattr(py_layout, name)
            == getattr(rs_layout, name)
        ), name
    for name in ("tag", "status", "cycles", "value"):
        c_val = h[f"KA_CQ_{name.upper()}"]
        assert (
            q["cq_offsets"][name]
            == c_val
            == getattr(py_layout, f"cq_{name}")
            == getattr(rs_layout, f"cq_{name}")
        ), name
    for name in ("magic", "version", "rings", "stdio", "epoch"):
        c_val = h[f"KA_QI_{name.upper()}"]
        assert (
            q["info_words"][name]
            == c_val
            == py_layout.info_words[name]
            == getattr(rs_layout, f"info_{name}")
        ), name
    for name in ("state", "abi", "done", "fatal"):
        c_val = h[f"KA_QF_{name.upper()}"]
        assert (
            q["fw_words"][name]
            == c_val
            == getattr(py_layout, f"fw_{name}")
            == getattr(rs_layout, f"fw_{name}")
        ), name
    assert py_layout.fw_bytes == len(q["fw_words"]) * 8
    for name in ("none", "ready", "stopped", "fatal"):
        c_val = h[f"KA_Q_ST_{name.upper()}"]
        assert (
            q["states"][name]
            == c_val
            == getattr(py_layout, f"st_{name}")
            == getattr(rs_layout, f"st_{name}")
        ), name
    for name in ("nop", "run", "stop", "list"):
        c_val = h[f"KA_SQ_{name.upper()}"]
        assert (
            q["opcodes"][name]
            == c_val
            == getattr(py_layout, f"op_{name}")
            == getattr(rs_layout, f"op_{name}")
        ), name
    for name in ("op", "tag", "pkg", "bytes", "bind", "timeout", "flags"):
        c_val = h[f"KA_SQ_RUN_{name.upper()}"]
        assert (
            q["run_words"][name]
            == c_val
            == getattr(py_layout, f"run_{name}")
            == getattr(rs_layout, f"run_{name}")
        ), name
    for name in ("op", "tag", "addr", "count", "epoch", "version", "flags"):
        c_val = h[f"KA_SQ_LIST_{name.upper()}"]
        rs_attr = "list_version_word" if name == "version" else f"list_{name}"
        assert (
            q["list_words"][name]
            == c_val
            == py_layout.list_words[name]
            == getattr(rs_layout, rs_attr)
        ), name
    for name in (
        "ok",
        "progress",
        "bad_op",
        "bad_arg",
        "unit_fault",
        "mover_fault",
        "timeout",
        "no_memory",
        "no_slots",
        "bad_free",
        "heap_busy",
    ):
        c_val = st[f"KA_ST_{name.upper()}"]
        assert (
            q["status"][name]
            == c_val
            == getattr(py_layout, name)
            == getattr(rs_layout, f"status_{name}")
        ), name
    s = q["record"]
    assert (
        s["bytes"] == h["KA_Q_REC_BYTES"] == py_layout.rec_bytes == rs_layout.rec_bytes
    )
    assert (
        s["version"]
        == h["KA_Q_REC_VERSION"]
        == py_layout.rec_version
        == rs_layout.rec_version
    )
    assert s["word"] == h["KA_Q_REC_WORD"] == py_layout.rec_word == rs_layout.rec_word
    assert {k: f["offset"] for k, f in s["fields"].items()} == firmware_record()[
        "fields"
    ]
    assert {f["width"] for f in s["fields"].values()} == {h["KA_Q_REC_WORD"]}
    for name in ("tag", "so_wr", "list_index", "item_tag", "epoch"):
        assert (
            s["fields"][name]["offset"]
            == h[f"KA_QR_{name.upper()}"]
            == py_layout.record.fields[name][0]
            == getattr(rs_layout, f"rec_{name}")
        ), name


def test_the_header_and_the_schema_give_the_same_record():
    assert not hasattr(L, "DEFAULT_SCHEMA") and not hasattr(L, "DEFAULT")
    h, st, q = header(), header(STATUS_H), machine_queue()
    py_layout = L.Layout.from_schema(q)
    rs_from_machine = _rs.QueueLayout(MACHINE.read_text(encoding="utf-8"))
    rs_from_py = _rs.QueueLayout(py_layout)
    assert_layout_matches(h, st, q, py_layout, rs_from_machine)
    assert_layout_matches(h, st, q, py_layout, rs_from_py)
    # Mutant witnesses: drifting any field in Python or Rust fails the comparison.
    for mutate in (
        lambda d: d["opcodes"].__setitem__("list", 5),
        lambda d: d["cq_offsets"].__setitem__("status", 16),
        lambda d: d["cq_offsets"].__setitem__("cycles", 8),
        lambda d: d.__setitem__("cq_status_mask", 0xFF),
        lambda d: d.__setitem__("op_mask", 0xFFFF),
        lambda d: d.__setitem__("sq_words", 4),
        lambda d: d.__setitem__("nbind_shift", 8),
        lambda d: d.__setitem__("stdio_mask", 0xFFFF),
        lambda d: d["fw_words"].__setitem__("state", 1),
        lambda d: d["status"].__setitem__("timeout", 0x99),
        lambda d: d["status"].__setitem__("unit_fault", 0x99),
        lambda d: d["status"].__setitem__("mover_fault", 0x99),
        lambda d: d["offsets"].__setitem__("so_wr", 0x200),
        lambda d: d["run_words"].__setitem__("pkg", 3),
        lambda d: d["record"].__setitem__("word", 16),
        lambda d: d.__setitem__("min_version", 2),
    ):
        drifted = json.loads(json.dumps(q))
        mutate(drifted)
        with pytest.raises((AssertionError, ValueError)):
            assert_layout_matches(
                h, st, q, L.Layout.from_schema(drifted), _rs.QueueLayout(drifted)
            )
    # Missing schema keys raise ValueError/KeyError rather than panicking or using defaults.
    missing_word = json.loads(json.dumps(q))
    del missing_word["record"]["word"]
    with pytest.raises(ValueError, match="word"):
        _rs.QueueLayout(missing_word)
    missing_offsets = json.loads(json.dumps(q))
    del missing_offsets["offsets"]
    with pytest.raises(ValueError, match="offsets"):
        _rs.QueueLayout(missing_offsets)
    with pytest.raises(TypeError, match="layout"):
        NodeQueue(MemoryTransport(), BASE)
    with pytest.raises(TypeError, match="layout"):
        SpecFirmware(MemoryTransport(), BASE)


def test_records_decode_with_the_schema_as_the_firmware_writes_them():
    s = schema_record()
    got = records(s)
    assert got == expected()

    # Mutant witness: a double that omits the RUN epoch fails full-record parity.
    class WrongRunEpoch(SpecFirmware):
        def write_record(self, slot, tag, time):
            self.epoch = 99
            super().write_record(slot, tag, time)

    assert records(s, double=WrongRunEpoch) != expected()
    # Full LIST record assertion (all 16 fields) and epoch update across discard_packages.
    mem = MemoryTransport()
    allocated = [0x80_0000]

    def spill(size):
        at = allocated[0]
        allocated[0] += size
        return at

    fw = SpecFirmware(mem, BASE, Interp(), record=firmware_record(), layout=LAYOUT)
    q = NodeQueue(
        mem,
        BASE,
        idle=fw.step,
        record=L.Record.from_schema(s),
        spill=spill,
        layout=LAYOUT,
    )
    q.init()
    fw.boot()
    pkg = q.prepare(bytes(PACKAGE_LINE))
    lst = q.prepare_list([(pkg, [], 0, 0, 7), (pkg, [], 0, 0, 9)])
    done = q.wait(q.submit_list(lst)).record
    assert done == {
        "tag": 1,
        "so_wr": 0,
        **{k: 30 + 2 * i for i, k in enumerate(TIMES)},
        "list_index": 2,
        "item_tag": 9,
        "epoch": 0,
    }
    lst.close()
    pkg.close()
    q.discard_packages()
    assert mem.read64(BASE + LAYOUT.epoch) == q._generation == 1
    after_discard = q.wait(q.submit(bytes(PACKAGE_LINE))).record
    assert after_discard == {
        "tag": 2,
        "so_wr": len(f"run {PACKAGE_LINE}B []\n"),
        **{k: 30 + i for i, k in enumerate(TIMES)},
        "list_index": 0,
        "item_tag": 0,
        "epoch": 1,
    }

    # Without a record ring (fw.record is None), step() never writes past si_at.
    def no_record_ring_witness(double_cls=SpecFirmware):
        m = MemoryTransport()
        f = double_cls(m, BASE, layout=LAYOUT)
        nq = NodeQueue(m, BASE, layout=LAYOUT)
        nq.init()
        f.boot()
        nq.submit(None, op=LAYOUT.op_list, words=(0, 0, 1, LAYOUT.list_version, 0))
        f.step()
        assert m.read_block(f.rec_at, LAYOUT.rec_bytes) == bytes(LAYOUT.rec_bytes)

    no_record_ring_witness()

    class AlwaysWritesListRecord(SpecFirmware):
        def step(self):
            self.record = firmware_record()
            super().step()

    with pytest.raises(AssertionError):
        no_record_ring_witness(AlwaysWritesListRecord)


def test_a_wrong_schema_offset_is_caught():
    bad = json.loads(json.dumps(schema_record()))
    f = bad["fields"]
    f["head"]["offset"], f["copy"]["offset"] = f["copy"]["offset"], f["head"]["offset"]
    assert records(bad) != expected()


def test_a_record_ring_out_of_step_with_the_cq_raises():
    class Skewed(SpecFirmware):
        def write_record(self, slot, tag, time):
            super().write_record((slot + 1) % self.cq_n, tag, time)

    with pytest.raises(RecordSkew):
        records(schema_record(), double=Skewed)


def test_a_version_3_region_has_no_record_ring():
    q = NodeQueue(MemoryTransport(), BASE, layout=LAYOUT)
    v4 = dict(q.geo)
    q.speak(LAYOUT.min_version)
    assert (
        q.geo["end"] == v4["rec"]
        and q.bind_off < -(-v4["end"] // LAYOUT.line) * LAYOUT.line
    )
    with pytest.raises(ValueError):
        q.speak(LAYOUT.min_version, L.Record.from_schema(schema_record()))
    with pytest.raises(ValueError):
        q.speak(LAYOUT.version + 1)
