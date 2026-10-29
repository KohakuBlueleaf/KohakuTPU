"""Retained node package, boot and bench runners on Rust-built packages."""

import contextlib
import importlib.util
import json
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from harness.fresh import track
from harness.registry import mutant, patched, runner

from kohakuaccel.driver.transport.base import TransportUnavailable
from kohakutpu.application.runtime import card as C
from kohakutpu.application.tools import l1 as L1
from kohakutpu.compiler import _rs
from kohakutpu.language.numerics.mxfp7 import value_fp16
from kohakutpu.stack import rs

ROOT = Path(__file__).resolve().parents[5]
INCLUDE = ROOT / "software/firmware/kohakuaccel/include/ka"
TESTS = ROOT / "software/application/tests"
BOOT_READY = 10.0
VLT_DEFINES = ()


@mutant("l0.bench/neg_go")
def negative_go():
    with patched(sys.modules[__name__], "VLT_DEFINES", ("NEG_GO",)):
        yield


@mutant("l0.bench/neg_wait")
def negative_wait():
    with patched(sys.modules[__name__], "VLT_DEFINES", ("NEG_WAIT",)):
        yield


@mutant("l0.card/timing_flag")
def timing_flag():
    with patched(C, "F_TIMING", 1):
        yield


@mutant("l0.card/firmware")
def firmware():
    with patched(C, "FIRMWARE", ROOT / "build/fw/kohakuaccel_node.elf"):
        yield


@mutant("l0.card/phases")
def phases():
    keep = C.NodeCard._result

    def result(self, q, done, pkg):
        got = keep(self, q, done, pkg)
        got["phases"] = {k: v + 1 for k, v in got["phases"].items()}
        return got

    with patched(C.NodeCard, "_result", result):
        yield


_CARDS = {}


def num(v):
    return int(v, 0) if isinstance(v, str) else int(v)


def hx(v):
    return f"{v:#x}"


def arg(v):
    if isinstance(v, str) and v.startswith("0x"):
        return int(v, 16)
    if isinstance(v, list):
        return [arg(x) for x in v]
    return v


def resolve(v, addrs):
    if isinstance(v, str) and v.startswith("$"):
        name, _, off = v[1:].partition("+")
        return addrs[name] + (num(off) if off else 0)
    if isinstance(v, list):
        return [resolve(x, addrs) for x in v]
    return arg(v)


def header(rel):
    text = re.sub(
        r"/\*.*?\*/", "", (INCLUDE / rel).read_text(encoding="utf-8"), flags=re.DOTALL
    )
    out = {}
    for name, value in re.findall(
        r"#define\s+(\w+)\s+\(?((?:0x[0-9A-Fa-f]+|\d+))[uUlL]*\)?\s*$",
        text,
        re.MULTILINE,
    ):
        out[name] = int(value, 0)
    for name, value, shift in re.findall(
        r"#define\s+(\w+)\s+\((\d+)[uUlL]*\s*<<\s*(\d+)\)\s*$", text, re.MULTILINE
    ):
        out[name] = int(value) << int(shift)
    for body in re.findall(r"enum\s*\w*\s*\{(.*?)\}", text, re.DOTALL):
        nxt = 0
        for item in body.split(","):
            m = re.match(r"\s*(\w+)\s*(?:=\s*(0x[0-9A-Fa-f]+|\d+))?", item)
            if not m or not m.group(1):
                continue
            nxt = int(m.group(2), 0) if m.group(2) else nxt
            out[m.group(1)] = nxt
            nxt += 1
    return out


def reorder(raw):
    h = list(struct.unpack_from("<12Q", raw))
    npay, nrel = h[3] & 0xFFFFFFFF, h[3] >> 32
    nbuf, nstep, nunit = h[4] & 0xFFFF, (h[4] >> 16) & 0xFFFF, (h[4] >> 32) & 0xFFFF
    sizes = [nunit * 16, nbuf * 32, nstep * 16, nrel * 16, npay * 32]
    tables = [raw[h[5 + i] : h[5 + i] + sizes[i]] for i in range(5)]
    out = bytearray(96)
    for i in reversed(range(5)):
        h[5 + i] = len(out)
        out += tables[i] + bytes(-len(tables[i]) % 32)
    h[2] = len(out) | (h[2] >> 32) << 32
    out[:96] = struct.pack("<12Q", *h)
    return bytes(out)


def stat(v):
    return {
        "magic": hx(v >> 48),
        "log2_depth": (v >> 40) & 0xFF,
        "log2_counters": (v >> 36) & 0xF,
        "overflow": (v >> 35) & 1,
        "clearing": (v >> 34) & 1,
        "enabled": (v >> 32) & 1,
    }


def stdout_report(text):
    """Unit/timing report shape; DBG.FAIL.006 checks the hardware snapshot."""
    pkgf = [
        re.sub(r"\d+", "N", ln)
        for ln in text.splitlines()
        if ln.startswith(("PKGF u", "PKGF stray "))
    ]
    pkgt = next(
        (ln.split()[1::2] for ln in text.splitlines() if ln.startswith("PKGT ")), []
    )
    vals = next(
        (
            dict(zip(ln.split()[1::2], map(int, ln.split()[2::2]), strict=False))
            for ln in text.splitlines()
            if ln.startswith("PKGT ")
        ),
        {},
    )
    ka = next(
        (
            re.sub(r"\b[0-9a-f]+\b", "H", ln.split("failed:")[1])
            for ln in text.splitlines()
            if ln.startswith("[ka] package ") and "failed:" in ln
        ),
        None,
    )
    return {"pkgf": pkgf, "pkgt_keys": pkgt, "pkgt": vals, "ka": ka}


@contextlib.contextmanager
def boot_timeout(card, cycles):
    if cycles is None:
        yield
        return
    keep = {i: a.timeout for i, (_, a) in card.boots.items()}
    for _, a in card.boots.values():
        a.timeout = cycles
    card.restart()
    try:
        yield
    finally:
        for i, (_, a) in card.boots.items():
            a.timeout = keep[i]
        card.restart()


def awaited(q, line):
    text = q.read_stdout()
    for _ in range(50):
        at = text.find(line)
        if at >= 0 and "\n" in text[at:]:
            break
        if q.idle is not None:
            q.idle()
        time.sleep(0.02)
        text += q.read_stdout()
    return text


def run_once(q, raw, timeout_cycles):
    tag = q.submit(raw, None, timeout_cycles=timeout_cycles)
    polls = max(1000, 16 * -(-(timeout_cycles or 2_000_000) // 2000))
    start = q._start()
    for _ in range(polls):
        q._waited(start, f"spec RUN {tag:#x}")
        c = q.take(tag)
        if c is not None:
            break
        if q.idle is not None:
            q.idle()
    else:
        raise TimeoutError(f"spec RUN {tag:#x} did not complete in {polls} polls")
    at_completion = q.console
    text = q.read_stdout() if c.ok else awaited(q, "[ka] package")
    return c, at_completion, text


def settled_done(q, polls=10_000):
    for _ in range(polls):
        if q._sq_head() == q.sq_tail:
            return q.state()["done"]
        if q.idle is not None:
            q.idle()
    raise TimeoutError(f"the SQ head stayed short of {q.sq_tail}")


def build_package(card, m, inp, addrs):
    builder = {
        "types": [
            [x, y, kind]
            for kind, coords in card.machine.units.items()
            for x, y in coords
        ],
        "credit": m.unit.inst_depth,
        "mesh": card.nodes[0],
        "fetch": card.fetch,
    }
    calls = [
        [call[0], *[resolve(a, addrs) for a in call[1:]]]
        for call in inp.get("calls", [])
    ]
    return _rs.package_build(
        json.dumps(builder),
        json.dumps(calls),
        card.machine_json,
        mesh=card.nodes[0],
        lower=inp.get("lower", False),
        mutant=rs._MUTANT.get(),
    )


@runner("l0.card.package")
def card_package(input, ctx):
    dev = ctx["session"].dev
    card, q, m = dev.card, dev.card.q, ctx["machine"]
    addrs = {}
    for name, b in input.get("buffers", {}).items():
        addrs[name] = dev.alloc(b["bytes"])
        dev.write(addrs[name], bytes.fromhex(b.get("init", "")) or bytes(b["bytes"]))
    phys = {k: dev.mmu.resolve(v)[1] for k, v in addrs.items()}
    raw = build_package(card, m, input, phys)
    steps = _rs.package_read(raw, card.machine_json, card.nodes[0])["steps"]
    if input.get("reorder"):
        raw = reorder(raw)
    report, out = input.get("report", ["status"]), {}
    with boot_timeout(card, input.get("boot_timeout")):
        q.read_stdout()
        done0 = settled_done(q)
        runs = [
            run_once(q, raw, input.get("timeout_cycles", 0))
            for _ in range(input.get("repeat", 1))
        ]
        done1 = settled_done(q)
    c, at_completion, text = runs[-1]
    if "status" in report:
        out.update(status=hx(c.status), detail=c.detail, step=c.step)
    if "value" in report:
        out["value"] = hx(c.value)
    if "stat" in report:
        out["stat"] = stat(c.value)
    if "steps" in report:
        out["n_steps"] = len(steps)
    rep = stdout_report(text)
    if "report" in report:
        out.update(pkgf=rep["pkgf"], pkgt_keys=rep["pkgt_keys"], ka=rep["ka"])
    if "timing" in report:
        out.update(pkgt_keys=rep["pkgt_keys"], local=rep["pkgt"].get("local"))
    if "order" in report:
        out["at_completion"] = {
            "pkgt": "PKGT " in at_completion,
            "pkgf": "PKGF " in at_completion,
        }
    if "done" in report:
        out["done"] = done1 - done0
    if "bounded" in report:
        bound = input.get("timeout_cycles") or input.get("boot_timeout")
        out["bounded"] = rep["pkgt"].get("steps", 0) >= bound
    for name, n in input.get("read", {}).items():
        out.setdefault("reads", {})[name] = dev.read(addrs[name], n).hex()
    for at in addrs.values():
        dev.free(at)
    return out


def rel(got, want):
    return float(np.abs(got - want).max() / np.abs(want).max())


@runner("l0.card.example")
def card_example(input, ctx):
    card = ctx["session"].dev.card
    name = input["example"]
    rng = np.random.default_rng(input.get("seed", 0))
    if name == "transpose":
        x = rng.standard_normal((8, 256)).astype(np.float16)
        y = L1.run(card, "transpose", {"x": x})["out"]["y"]
        want = x.reshape(8, 16, 16).transpose(1, 0, 2).reshape(16, 128)
        return {"equal": bool(np.array_equal(y, want))}
    if name == "memops":
        o = L1.run(card, "memops", {})["out"]["out"].astype(np.uint64)
        return {"o0": hx(int(o[0])), "o1": hx(int(o[1]))}
    if name == "silu":
        x = rng.standard_normal(65536).astype(np.float16)
        y = L1.run(card, "silu", {"x": x})["out"]["y"]
        v = x.astype(np.float64)
        return {"within": rel(y, v / (1 + np.exp(-v))) < input["tolerance"]}
    if name == "tonode":
        x = rng.standard_normal((8, 64)).astype(np.float16)
        w = (rng.standard_normal((16, 64)) / 8).astype(np.float16)
        z = L1.run(card, "tonode", {"x": x, "w": w}, reps=1)["out"]["z"]
        want = value_fp16(x) @ value_fp16(w).T
        want = want.reshape(2, 4, 4, 4).transpose(0, 2, 1, 3).reshape(8, 16)
        return {"within": rel(z[16:].reshape(8, 16), want) < input["tolerance"]}
    raise ValueError(f"no example check {name}")


@runner("l0.card.boot", shaped=True)
def card_boot(input, ctx):
    card = ctx["session"].dev.card
    q = card.q
    card.restart(ready=BOOT_READY)
    text = awaited(q, " ready: ")
    banner = next((ln for ln in text.splitlines() if " ready: " in ln), "")
    m = re.search(r"(\d+) units, sig (\w+), classes ([^;]*);", banner)
    _, args = card.boots[card.nodes[0]]
    st = q.state()
    return {
        "state": st["state"],
        "abi": card.abi["boot"] if st["abi"] == q.version else st["abi"],
        "units": sorted([list(u) for u in q.units()]),
        "banner_units": int(m[1]) if m else None,
        "banner_classes": m[3].split() if m else None,
        "queue_is_region": args.queue == q.base,
        "mesh": args.mesh,
        "timing": bool(args.flags & 2),
    }


def verilated(nodes):
    if nodes not in _CARDS:
        env = "KTPU_SPEC_VERILATOR" if nodes == 1 else "KTPU_SPEC_VERILATOR2"
        build = os.environ.get(env)
        if not build:
            raise TransportUnavailable(f"tier 2: {env} names no Verilated card build")
        board = os.environ.get("KTPU_BOARD", "multimesh_v9")
        _CARDS[nodes] = track(
            C.VerilatorCard(build, board=board, nodes=tuple(range(nodes)))
        )
    return _CARDS[nodes]


@runner("l0.bench")
def bench(input, ctx):
    if "vlt" in input:
        cmd = [sys.executable, str(ROOT / "scripts/py/vlt.py"), input["vlt"]]
        for d in VLT_DEFINES:
            cmd += ["--define", d]
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
        return {"outcome": "passed" if r.returncode == 0 else r.stdout[-400:]}
    path, _, fn = input["test"].partition("::")
    spec = importlib.util.spec_from_file_location(Path(path).stem, TESTS / path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    card = verilated(input.get("nodes", 1))
    try:
        getattr(mod, fn)(card, **input.get("args", {}))
    except TransportUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - any bench failure is the recorded outcome
        return {"outcome": f"{type(e).__name__}: {e}"[:400]}
    return {"outcome": "passed"}
