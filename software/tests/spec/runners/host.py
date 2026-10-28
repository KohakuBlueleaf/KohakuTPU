"""Host queue, firmware metadata and runtime parsing witnesses."""

import importlib.util
import re
import struct
from pathlib import Path
from types import SimpleNamespace

from harness.registry import mutant, patched, runner

from kohakuaccel.driver.node.boot import args as A
from kohakuaccel.driver.node.queue import client as QC
from kohakuaccel.driver.node.queue import layout as QL
from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF
from kohakutpu.stack import rs

from .target.l0 import INCLUDE, ROOT, header, hx, num

FORMAT_OVERRIDES = SimpleNamespace(values={})
MODULES = {"layout": QL, "client": QC, "args": A, "card": C}
LINKMAP = ROOT / "software/firmware/kohakuaccel/arch/rv64/node.ld"


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FWB = load_file("ka_fw_build", ROOT / "software/firmware/build.py")


def refused(e):
    return {"error": type(e).__name__, "message": str(e)}


class Mem:
    """Host memory with a log of each queue access."""

    def __init__(self, base, size):
        self.base, self.data, self.log = base, bytearray(size), []

    def write_block(self, at, raw):
        self.log.append(["w", hx(at - self.base), len(raw)])
        self.put(at - self.base, raw)

    def read_block(self, at, n):
        self.log.append(["r", hx(at - self.base), n])
        return bytes(self.data[at - self.base : at - self.base + n])

    def read64(self, at):
        self.log.append(["r", hx(at - self.base), 8])
        return struct.unpack_from("<Q", self.data, at - self.base)[0]

    def put(self, off, raw):
        self.data[off : off + len(raw)] = raw

    def word(self, off):
        return struct.unpack_from("<Q", self.data, off)[0]


class Firmware:
    """Queue producer using offsets from firmware headers, not host constants."""

    def __init__(self, mem, sq_n, cq_n, so_bytes):
        h = {**header("queue/layout.h"), **header("package/status.h")}
        self.h, self.mem = h, mem
        self.cq_n, self.so_bytes = cq_n, so_bytes
        cq = h["KA_Q_SQ"] + sq_n * h["KA_Q_SQ_BYTES"]
        line = h["KA_Q_LINE"]
        self.cq_at = cq
        self.so_at = -(-(cq + cq_n * h["KA_Q_CQ_BYTES"]) // line) * line
        self.cq = self.done = self.so = 0

    def store(self, off, *words):
        self.mem.put(off, struct.pack(f"<{len(words)}Q", *words))

    def complete(self, tag, status=0, detail=0, step=0, cycles=0, value=0):
        h = self.h
        slot = self.cq_at + (self.cq % self.cq_n) * h["KA_Q_CQ_BYTES"]
        self.store(slot, tag, status | detail << 16 | step << 32, cycles, value)
        self.cq += 1
        self.store(h["KA_Q_CQ_TAIL"], self.cq)
        if status != h["KA_ST_PROGRESS"]:
            self.done += 1
            self.store(h["KA_Q_SQ_HEAD"], self.done)

    def print(self, text):
        for ch in text.encode():
            self.mem.put(self.so_at + self.so % self.so_bytes, bytes([ch]))
            self.so += 1
        self.store(self.h["KA_Q_SO_WR"], self.so)


def completion(c):
    return {
        "tag": c.tag,
        "status": hx(c.status),
        "detail": c.detail,
        "step": c.step,
        "cycles": c.cycles,
        "value": hx(c.value),
    }


@runner("l0.queue")
def queue(inp, ctx):
    base = num(inp.get("base", "0x8000000000"))
    size = num(inp.get("size", QC.REGION_BYTES))
    mem = Mem(base, size)
    q = QC.NodeQueue(mem, base, size=size, **inp.get("geometry", {}))
    fw = Firmware(mem, q.sq_n, q.cq_n, q.so_bytes)
    line, out = fw.h["KA_Q_LINE"], []
    for act in inp["script"]:
        do = act["do"]
        if do == "fill":
            mem.put(0, bytes([0xA5]) * act["n"])
            out.append(None)
        elif do == "zero":
            at = num(act["at"])
            out.append(not any(mem.data[at : at + act["n"]]))
        elif do == "whole_lines":
            out.append(
                all(
                    int(off, 16) % line == 0 and n % line == 0
                    for kind, off, n in mem.log
                    if kind == "w"
                )
            )
        elif do == "init":
            q.init()
            out.append(None)
        elif do == "submit":
            pkg = bytes.fromhex(act["package"]) if "package" in act else None
            if "length" in act:
                pkg = bytes(act["length"])
            binds = [num(b) for b in act["bindings"]] if "bindings" in act else None
            try:
                out.append(
                    q.submit(
                        pkg,
                        binds,
                        timeout_cycles=act.get("timeout", 0),
                        flags=act.get("flags", 0),
                    )
                )
            except ValueError as e:
                out.append(refused(e))
        elif do == "complete":
            fw.complete(
                act["tag"],
                num(act.get("status", 0)),
                act.get("detail", 0),
                act.get("step", 0),
                act.get("cycles", 0),
                num(act.get("value", 0)),
            )
            out.append(None)
        elif do == "print":
            fw.print(act["text"])
            out.append(None)
        elif do == "state":
            fw.store(fw.h["KA_Q_FW"], *[num(w) for w in act["words"]])
            out.append(None)
        elif do == "poll":
            out.append([completion(c) for c in q.poll()])
        elif do == "wait":
            try:
                out.append(completion(q.wait(act["tag"], timeout=1.0)))
            except QC.NodeError as e:
                out.append(
                    {
                        "error": "NodeError",
                        "message": str(e),
                        "completion": completion(e.completion),
                        "console": e.console,
                    }
                )
        elif do == "wait_ready":
            try:
                out.append(q.wait_ready(timeout=act.get("timeout", 0.01)))
            except (QC.BootRefused, QC.FirmwareFatal, TimeoutError) as e:
                out.append(refused(e))
        elif do == "progress":
            out.append([completion(c) for c in q.progress])
        elif do == "stdout":
            out.append(q.read_stdout())
        elif do == "upload":
            try:
                out.append(hx(q.upload(bytes(act["length"])) - base))
            except ValueError as e:
                out.append(refused(e))
        elif do == "read":
            out.append(
                bytes(mem.data[num(act["at"]) : num(act["at"]) + act["n"]]).hex()
            )
        elif do == "words":
            at = num(act["at"])
            out.append([hx(mem.word(at + 8 * k)) for k in range(act["n"])])
        elif do == "geometry":
            out.append({k: hx(v) for k, v in q.geo.items()})
        elif do == "offsets":
            out.append({"bind": hx(q.bind_off), "heap": hx(q.heap_off)})
        elif do == "log":
            out.append(mem.log)
            mem.log = []
        else:
            raise ValueError(f"no queue action {do}")
    return out


@runner("l0.queue.geometry")
def queue_geometry(inp, ctx):
    try:
        got = QL.geometry(*inp["args"])
    except ValueError as e:
        return refused(e)
    return {k: hx(v) for k, v in got.items()}


def host_value(key, machine, name):
    match = re.fullmatch(r"(\w+)\.(\w+)(?:\[(.+)\])?", key)
    if match is None:
        raise KeyError(key)
    mod, attr = match[1], match[2]
    if mod in ("format", "engine"):
        machine = machine or MF.load(name)
        fmt = _rs.package_format(rs.machine_json(name))
        fmt.update(
            MAGIC=machine.fw.package.magic,
            VERSION=machine.fw.package.version,
            HEADER_BYTES=machine.fw.package.header_bytes,
            UNITS=machine.fw.engine_units,
        )
        v = FORMAT_OVERRIDES.values.get(attr, fmt[attr])
    else:
        v = getattr(MODULES[mod], attr)
    if match[3] is not None:
        v = v[num(match[3])]
    return str(v) if isinstance(v, Path) else v


@runner("l0.pairs")
def pairs(inp, ctx):
    out = {}
    for key in inp.get("host", []):
        v = host_value(key, ctx["machine"], ctx["vector"].get("machine", rs.BASE))
        out[key] = {str(k): x for k, x in v.items()} if isinstance(v, dict) else v
    for key in inp.get("header", []):
        rel, name = key.split(":")
        if name.startswith("struct "):
            text = (INCLUDE / rel).read_text(encoding="utf-8")
            body = re.search(
                r"struct\s+" + name.split()[1] + r"\s*\{(.*?)\};", text, re.DOTALL
            )
            if body is None:
                raise KeyError(name)
            out[key] = re.findall(
                r"\b(\w+)(?:\[\w+\])?\s*;", re.sub(r"/\*.*?\*/", "", body[1])
            )
        else:
            out[key] = header(rel)[name]
    for key in inp.get("machine", []):
        v = ctx["machine"].get(key)
        out[key] = v.dict() if hasattr(v, "dict") else v
    return out


@runner("l0.table")
def table(inp, ctx):
    host = {int(k): v for k, v in QL.WAITS.items()}
    head = {
        v: k.removeprefix("KA_WAIT_")
        for k, v in header("package/status.h").items()
        if k.startswith("KA_WAIT_")
    }
    out = {
        "host_only": sorted(set(host) - set(head)),
        "header_only": sorted(set(head) - set(host)),
        "renamed": sorted(
            c
            for c in set(host) & set(head)
            if host[c].upper().replace(" ", "_") != head[c]
        ),
    }
    if need := inp.get("need"):
        out["need"] = {
            "host": any(v.upper() == need for v in host.values()),
            "header": need in head.values(),
        }
    return out


@runner("l0.firmware")
def firmware(inp, ctx):
    m = ctx["machine"]
    link = {
        name: [int(origin, 16), int(length) * 1024]
        for name, origin, length in re.findall(
            r"(\w+)\s*\(\w+\)\s*:\s*ORIGIN\s*=\s*(0x[0-9A-Fa-f]+),\s*LENGTH\s*=\s*(\d+)K",
            LINKMAP.read_text(encoding="utf-8"),
        )
    }
    out = {
        "card_image": Path(C.FIRMWARE).relative_to(ROOT).as_posix(),
        "machine_image": m.fw.image,
        "parts": FWB.IMAGES[m.fw.image],
        "machine_parts": list(m.fw.parts),
        "machine_classes": list(m.fw.unit_classes),
        "link": link,
        "build_memory": [FWB.IMEM_BYTES, FWB.SPAD_BYTES],
        "machine_memory": [m.node.imem_bytes, m.node.spad_bytes],
    }
    return {k: out[k] for k in inp.get("keys", out)}


@runner("l0.card.parse")
def card_parse(inp, ctx):
    done = QC.Completion(1, 0, 0, 0, inp.get("cycles", 0), 0)
    got = C.NodeCard._result(None, inp["stdout"], done, SimpleNamespace(steps=[]))
    return {"cycles": got["cycles"], "phases": got["phases"], "steps": got["steps"]}


@runner("l0.card.layout")
def card_layout(inp, ctx):
    spec = machine_of(ctx["vector"]["machine"])
    return {
        "queues": [
            hx(spec.stage_addr(0, mesh=i)) for i in range(ctx["machine"].card.nodes)
        ],
        "size": hx(C.Q_SIZE),
        "rings": C.RING,
    }


def constant_mutant(name, obj, attr, value):
    @mutant(name)
    def changed():
        with patched(obj, attr, value):
            yield


for _name, _obj, _attr, _value in [
    ("line32", QL, "LINE", 32),
    ("magic", QL, "MAGIC", 0x324555455551414B),
    ("sq_tail", QL, "SQ_TAIL", 0x0C0),
    ("cq_entry", QL, "CQ_BYTES", 64),
    ("sq_entry", QL, "SQ_BYTES", 32),
    ("sq_base", QL, "SQ", 0x400),
    ("status_names", QL, "STATUS", {**QL.STATUS, 0x30: "STALL", 0: "DONE"}),
    ("wait_names", QL, "WAITS", {**QL.WAITS, 2: "engine"}),
    ("progress_final", QL, "PROGRESS", 0x99),
    ("so_offset", QL, "SO_WR", 0x1C0),
    ("bindings", QC, "MAX_BINDINGS", 32),
    ("ok_status", QL, "OK", 1),
    ("states", QL, "STATES", {**QL.STATES, 1: "stopped", 2: "ready"}),
    ("region_size", C, "Q_SIZE", 0x8_0000),
]:
    constant_mutant(f"l0.queue/{_name}", _obj, _attr, _value)

constant_mutant("l0.fw/memory_budget", FWB, "IMEM_BYTES", 16 * 1024)
constant_mutant("l0.table/drop_wait", QL, "WAITS", dict(list(QL.WAITS.items())[1:]))
constant_mutant(
    "l0.table/no_engine",
    QL,
    "WAITS",
    {k: v for k, v in QL.WAITS.items() if v != "engine"},
)


constant_mutant("l0.boot/magic", A, "MAGIC", 0x32544F4F42414B)
constant_mutant("l0.boot/max_units", A, "MAX_UNITS", 15)


@mutant("l0.boot/ignore_refusal")
def ignore_refusal():
    keep = QC.NodeQueue._check_ready_state

    def check_ready(self, st):
        if st["state"] == "fatal":
            return None
        return keep(self, st)

    with patched(QC.NodeQueue, "_check_ready_state", check_ready):
        yield


for _name, _attr, _value in [
    ("l0.lower/wait_code", "WAIT", 0x21),
    ("l0.lower/no_rel", "REL", 0),
    ("l0.lower/ctr_mask", "CTR_MASK", 255),
    ("l0.lower/src_mover", "SRC_MOVER", 0),
    ("l0.pkg/magic", "MAGIC", 0),
    ("l0.pkg/version", "VERSION", 0),
    ("l0.pkg/header_bytes", "HEADER_BYTES", 32),
    ("l0.pkg/regs", "REGS", 8),
]:
    constant_mutant(_name, FORMAT_OVERRIDES, "values", {_attr: _value})


@mutant("l0.queue/no_console")
def no_console():
    def read_stdout(self):
        self._stdout()
        self.console = ""
        return ""

    with patched(QC.NodeQueue, "read_stdout", read_stdout):
        yield


@mutant("l0.queue/default_ring")
def default_ring():
    d = list(QC.NodeQueue.__init__.__defaults__ or ())
    d[3] = 2048
    with patched(QC.NodeQueue.__init__, "__defaults__", tuple(d)):
        yield


@mutant("l0.queue/no_tags")
def no_tags():
    keep = QC.NodeQueue.submit

    def submit(self, *a, **kw):
        tag = keep(self, *a, **kw)
        self._tag -= 1 if tag > 1 else 0
        return tag

    with patched(QC.NodeQueue, "submit", submit):
        yield
