"""Runners for the debug firmware (docs/spec/stack/debug/firmware.md s2).

Tier 1 runs on the card session's first node with the debug image booted for
the vector (`NodeCard.load(debug=True)`) and the production image booted
again after it, so the session's other vectors see the image they expect."""

import contextlib
import json
import os
import tempfile
from itertools import pairwise
from pathlib import Path

import numpy as np
from harness.registry import mutant, patched, runner

from kohakuaccel.driver.device import mover as MV
from kohakuaccel.driver.node.boot import abi
from kohakuaccel.driver.node.boot import args as A
from kohakuaccel.driver.node.boot import loader as LD
from kohakuaccel.driver.node.queue import client as QC
from kohakuaccel.driver.node.queue import layout as QL
from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime import session as S
from kohakutpu.application.tools import l1 as L1
from kohakutpu.language import examples
from runners.target.l0 import header, num, run_once, stdout_report


# ----------------------------------------------------------------- tier 0
@runner("dbg.bound")
def bound(input: dict, ctx: dict):
    """`args.bound`: a wait of `seconds` in sysnode cycles at each named
    profile's sys clock (the machine's `card.profiles`)."""
    profiles = ctx["machine"].get("card.profiles").dict()
    return {p: A.bound(input["seconds"], profiles[p]["sys"]) for p in input["profiles"]}


def region(records: list, units: int = 32, last: int = 4, cap: int = 64 * 8) -> bytes:
    """A debug region as debug.c lays it out (offsets from ka/package/debug.h),
    holding `records` (8 words each) from the host's read position 0."""
    h = header("package/debug.h")
    rb = h["KA_DBG_REC_BYTES"]
    rec = (h["KA_DBG_LASTS"] + units * (1 + last) * 8 + 63) & ~63
    b = bytearray(rec + cap)
    for at, v in (
        ("MAGIC", h["KA_DBG_MAGIC"]),
        ("H_VERSION", h["KA_DBG_VERSION"]),
        ("H_LAST", last),
        ("H_UNITS", units),
        ("H_REC", rec),
        ("H_RECCAP", cap),
        ("H_WR", rb * len(records)),
    ):
        off = 0 if at == "MAGIC" else h[f"KA_DBG_{at}"]
        b[off : off + 8] = v.to_bytes(8, "little")
    for i, words in enumerate(records):
        b[rec + i * rb : rec + (i + 1) * rb] = b"".join(
            w.to_bytes(8, "little") for w in words
        )
    return bytes(b)


@runner("dbg.decode")
def decode(input: dict, ctx: dict):
    """The host decoder (`_rs.debug_decode`) on a region of the given
    records (``[w0, tag]`` padded to 8 words): the record types it read, or
    the clause it refused by."""
    recs = [[num(w) for w in r] + [0] * (8 - len(r)) for r in input["records"]]
    raw = region(recs)
    if input.get("no_header"):
        raw = bytes(len(raw))
    text = (C.MACHINES / f"{ctx['vector']['machine']}.json").read_text(encoding="utf-8")
    try:
        got = C._rs.debug_decode(raw, text)
    except C._rs.Refused as e:
        return {"refused": e.clause}
    return {
        "packages": [
            [p["tag"], len(p["steps"]), len(p["units"]), p["fail"] is not None]
            for p in got["packages"]
        ]
    }


@runner("dbg.drained")
def drained(input: dict, ctx: dict):
    """Whether the host marks a failure dump stable (DBG.DUMP.002): from a
    debug region holding one FAIL record of each flag word in ``flags``, and
    from each production-image stdout text in ``consoles``."""
    text = (C.MACHINES / f"{ctx['vector']['machine']}.json").read_text(encoding="utf-8")
    fail = header("package/debug.h")["KA_DBG_FAIL"]
    reports = [
        C._rs.debug_decode(region([[fail | num(f) << 8, 1] + [0] * 6]), text)
        for f in input["flags"]
    ]
    return {
        "report": [C.NodeCard.drained(r) for r in reports],
        "console": [C.NodeCard.drained(None, t) for t in input["consoles"]],
    }


# ----------------------------------------------------------------- tier 1
@contextlib.contextmanager
def debug_image(card):
    """The debug image booted on every node for the duration, then the
    production image again."""
    card.load(debug=True)
    try:
        yield card
    finally:
        card.load(debug=False)


def package_builder(card, m):
    return {
        "types": [
            [x, y, kind]
            for kind, coords in card.machine.units.items()
            for x, y in coords
        ],
        "credit": m.unit.inst_depth,
        "mesh": card.nodes[0],
        "fetch": card.fetch,
    }


def failing(card, m, lower: bool = True) -> bytes:
    """A package whose AWAIT on unit (1, 1) never completes: lowered for the
    dispatch engine as the card runtime sends it, or (`lower` False) as the
    firmware path runs it."""
    return C._rs.package_build(
        json.dumps(package_builder(card, m)),
        json.dumps([["unit", [1, 1]], ["await_", 0, 1]]),
        card.machine_json,
        mesh=card.nodes[0],
        lower=lower,
    )


def stalled(card, m, writes: int) -> bytes:
    """`failing`'s engine package with `writes` mailbox ARG writes queued
    ahead of its WAIT: the engine issues them, then stalls (DBG.FAIL.005).
    No GO follows, so nothing is sent; every send writes all four ARGs."""
    pkg = C._rs.package_build(
        json.dumps(package_builder(card, m)),
        json.dumps([["unit", [1, 1]]]),
        card.machine_json,
        mesh=card.nodes[0],
    )
    codes = C._rs.package_format(card.machine_json, card.nodes[0])
    entries = [["put", codes["ARG0"] + k % 4, k] for k in range(writes)]
    entries.append(["wait", 0, 1])
    return C._rs.package_stream(
        pkg, json.dumps(entries), card.machine_json, card.nodes[0]
    )


@runner("dbg.snapshot")
def snapshot(input: dict, ctx: dict):
    """A completed full-line fill, then a WAIT without a completion; both images
    must retain pre-clear state. The fault case issues an unsupported mode."""
    card = ctx["session"].dev.card
    debug = input["debug"]
    image = C.DEBUG_FIRMWARE if debug else C.FIRMWARE
    card.load(fw=image, debug=debug)
    at = card.alloc(64)
    try:
        raw = C._rs.package_build(
            json.dumps(package_builder(card, ctx["machine"])),
            json.dumps([["unit", [1, 1]]]),
            card.machine_json,
            mesh=card.nodes[0],
        )
        if input.get("fault"):
            entries = [["put", 8, (1 << 16) | MV.TRANSPOSE]]
        else:
            writes = MV.fill(MV.Walker(at, [(2, 32)]), 0x1234)
            entries = [["put", 8 + off // 8, value] for off, value in writes]
            entries.append(["wait", 16, 1])
        entries.extend([["put", 1 + k, k] for k in range(4)])
        entries.append(["wait", 0, 1])
        raw = C._rs.package_stream(
            raw, json.dumps(entries), card.machine_json, card.nodes[0]
        )
        card.q.read_stdout()
        try:
            card.q.run(raw, timeout_cycles=input["timeout_cycles"])
        except QC.NodeError as error:
            snap = error.snapshot
            if snap is None:
                return {"snapshot": False, "error": error.snapshot_error}
            with tempfile.TemporaryDirectory() as directory:
                with patched(os, "environ", {**os.environ, C.FAILED_ENV: directory}):
                    pkg = C.B.Package(raw, card.machine_json, card.nodes[0])
                    card._keep_failed(pkg, [], error, card.nodes[0])
                (record,) = Path(directory).glob("*.json")
                saved = json.loads(record.read_text(encoding="utf-8"))
                persisted = (
                    saved["snapshot"] == snap
                    and saved["completion"]["tag"] == error.completion.tag
                )
            stat, moves, mover = snap["stat"], snap["moves"], snap["mover"]
            out = {
                "snapshot": True,
                "persisted": persisted,
                "status": error.completion.name,
                "enabled": stat["enabled"],
                "magic": stat["magic"],
                "counters_present": len(snap["counters"]) == stat["counter_count"],
                "consistent": moves["issued"] >= moves["done"],
                "gos": moves["issued"],
                "fault": mover["fault"],
            }
            if not input.get("fault"):
                out.update(
                    blocked=stat["head_blocked"],
                    issued=stat["issued"],
                    expected_issued=len(entries) - 1,
                    done=moves["done"],
                    same_stat=snap["raw"]["stat"] == error.completion.value,
                    room=mover["room"],
                    busy=mover["busy"],
                    counters=snap["counters"],
                )
            return out
        raise AssertionError("DBG.FAIL.006: package unexpectedly completed")
    finally:
        card.free(at)
        card.load(fw=C.ROOT / "build/fw/kohakutpu_node.elf", debug=False)


@mutant("dbg/snapshot_after_clear")
def m_snapshot_after_clear():
    """Boot real firmware whose first-failure capture is deferred until report,
    after de_end clears queue/counters and rebases MOVES."""
    with (
        patched(C, "FIRMWARE", C.ROOT / "build/fw/kohakutpu_snapshot_mutant.elf"),
        patched(
            C, "DEBUG_FIRMWARE", C.ROOT / "build/fw/kohakutpu_snapshot_debug_mutant.elf"
        ),
    ):
        yield


def flags_of(names) -> int:
    """Run flags by name (`layout.RUN_F_<name>`)."""
    out = 0
    for f in names:
        out |= getattr(QL, f"RUN_F_{f}")
    return out


@runner("dbg.trace")
def trace(input: dict, ctx: dict):
    """An L1 example traced on the debug image: per run (``runs``: name ->
    extra run flags), its step records against the package's steps."""
    card = ctx["session"].dev.card
    out: dict = {}
    with debug_image(card):
        for name, extra in input["runs"].items():
            got = L1.run(
                card, input["example"], {}, reps=1, trace=True, flags=flags_of(extra)
            )
            pkg, rep = got["pkg"], got["debug"]
            steps = [s for p in rep["packages"] for s in p["steps"]]
            covered = [i for s in steps for i in range(s["first"], s["last"] + 1)]
            out[name] = {
                "records": len(steps),
                "covers": covered == list(range(len(pkg.steps))),
                "ops": [s["op"] for s in steps]
                == [pkg.steps[s["first"]].op.name for s in steps],
                "rising": all(a["cycle"] <= b["cycle"] for a, b in pairwise(steps)),
                "clocks": sorted({s["clock"] for s in steps}),
                "lost": rep["header"]["lost"],
            }
    return out


@runner("dbg.same")
def same(input: dict, ctx: dict):
    """The transpose example's output on the debug image, traced and not,
    against numpy."""
    card = ctx["session"].dev.card
    rng = np.random.default_rng(input.get("seed", 0))
    x = rng.standard_normal((8, 256)).astype(np.float16)
    want = x.reshape(8, 16, 16).transpose(1, 0, 2).reshape(16, 128)
    with debug_image(card):
        plain = L1.run(card, "transpose", {"x": x}, reps=1)["out"]["y"]
        traced = L1.run(card, "transpose", {"x": x}, reps=1, trace=True)["out"]["y"]
    return {
        "plain": bool(np.array_equal(plain, want)),
        "traced": bool(np.array_equal(traced, want)),
    }


@runner("dbg.fail")
def fail(input: dict, ctx: dict):
    """A package that times out on the debug image: its failure report as
    the host holds it when the completion arrives, the RUL.075 rerun, the
    firmware restarts meanwhile, and a following example run on the same
    boot; ``keys`` picks the parts."""
    card = ctx["session"].dev.card
    bound_ = input["timeout_cycles"]
    writes = input.get("writes", 0)
    raw = (
        stalled(card, ctx["machine"], writes)
        if writes
        else failing(card, ctx["machine"])
    )
    q = card.q
    out: dict = {}
    boots = []
    keep = LD.NodeBoot.restart

    def restart(self, *a, **kw):
        boots.append(self.mesh)
        return keep(self, *a, **kw)

    with debug_image(card), patched(LD.NodeBoot, "restart", restart):
        tag = q.submit(raw, None, timeout_cycles=bound_)
        c = q.take(tag)
        while c is None:
            c = q.take(tag)
        # Read before anything else polls the node (DBG.FAIL.002).
        rep = card.debug_report()
        pkg = next((p for p in rep["packages"] if p["tag"] == tag), None)
        f = pkg["fail"] if pkg is not None else None
        out["at_completion"] = f is not None
        if pkg is not None and f is not None:
            u = pkg["units"][0]["unit"] if pkg["units"] else None
            out["report"] = {
                "status": f["status_name"],
                "detail": f["detail_name"],
                "same_as_completion": [f["status"], f["detail"], f["step"]]
                == [c.status, c.detail, c.step],
                "op": f["op"],
                "unit": f["unit"],
                "unit_ref": u and [u["kind"], u["mesh"], u["x"], u["y"]],
                "units": f["units"],
                "engine": f["engine"],
                "drained": f["drained"],
                "mover_fault": f["mover"]["fault_name"],
            }
        if f is not None and "engine_report" in input.get("keys", ()):
            # The engine's issue count beside the queued step (DBG.FAIL.005),
            # against the STAT word the AWAIT timeout's completion carries.
            out["engine_report"] = {
                "engine": f["engine"],
                "step": f["step"],
                "op": f["op"],
                "issued": f["issued"],
                "issued_is_stat": f["issued"] == (c.value >> 16) & 0xFFFF,
            }
        plain = failing(card, ctx["machine"], lower=False)
        again = card._debug_rerun(card.nodes[0], plain, None, rep, bound_)
        r = next((p for p in again["packages"] if p["fail"]), None)
        out["rerun"] = r and {
            "engine": r["fail"]["engine"],
            "status": r["fail"]["status_name"],
            "op": r["fail"]["op"],
            "step": r["fail"]["step"],
            "counters": [u["counters"] for u in r["units"]],
        }
        o = L1.run(card, "memops", {}, reps=1)["out"]["out"].astype(np.uint64)
        out["next"] = [hex(int(o[0])), hex(int(o[1]))]
    out["restarts"] = len(boots)
    return {k: out.get(k) for k in input.get("keys", out)}


@runner("dbg.dump")
def dump(input: dict, ctx: dict):
    """An L1 example run whole and cut after step ``after`` (RUL.076), each
    with every parameter dumped: per run, each dump's size, stability and
    first ``words`` 64-bit words, and whether the whole run's dump is what the
    example's own read-back holds."""
    card = ctx["session"].dev.card
    out: dict = {}
    for name, after in (("whole", None), ("cut", input["after"])):
        got = L1.run(card, input["example"], {}, reps=1, dump=True, after=after)
        out[name] = {
            p: {
                "bytes": len(d["bytes"]),
                "stable": d["stable"],
                "words": [
                    hex(int(w))
                    for w in np.frombuffer(d["bytes"], np.uint64)[: input["words"]]
                ],
            }
            for p, d in got["dump"].items()
        }
        if after is None:
            m = examples.load(examples.example(input["example"]))
            layout = dict(L1.bufs(m, m.names()[0], card.machine_json))
            out["read_back"] = all(
                d["bytes"] == L1.raw(layout[p], got["out"][p])
                for p, d in got["dump"].items()
            )
    return out


@runner("dbg.session")
def session(input: dict, ctx: dict):
    """A debug session of ``seconds``: memops, the failing package (its own
    ``timeout_cycles`` bound), memops again; each run's outcome, the failure's
    report as recorded, and whether the session kept its budget."""
    card = ctx["session"].dev.card
    raw = failing(card, ctx["machine"], lower=False)

    def memops():
        o = L1.run(card, "memops", {}, reps=1)["out"]["out"].astype(np.uint64)
        return [hex(int(o[0])), hex(int(o[1]))]

    def fail():
        return card.q.run(
            raw,
            None,
            timeout=card.timeout,
            timeout_cycles=input["timeout_cycles"],
            flags=QL.RUN_F_NOENGINE,
        )

    with S.DebugSession(card, seconds=input["seconds"]) as s:
        for fn in (memops, fail, memops):
            s.attempt(fn)
        kept = s.left() > 0
    bad = s.records[1]
    f = next(p["fail"] for p in bad["debug"]["packages"] if p["fail"])
    return {
        "ok": [r["ok"] for r in s.records],
        "values": [s.records[0]["result"], s.records[2]["result"]],
        "failure": [f["status_name"], f["detail_name"], bad["drained"]],
        "within_budget": kept,
        "image_after": "debug" if card.debug else "production",
    }


@runner("dbg.tags")
def tags(input: dict, ctx: dict):
    """Traced runs of the memops example: the tag its records carry against
    the tag its submission was given."""
    card = ctx["session"].dev.card
    seen, given = [], []
    keep = QC.NodeQueue.submit

    def submit(self, *a, **kw):
        tag = keep(self, *a, **kw)
        given.append(tag)
        return tag

    with debug_image(card), patched(QC.NodeQueue, "submit", submit):
        for _ in range(input["runs"]):
            got = L1.run(card, "memops", {}, reps=1, trace=True)
            seen.append([p["tag"] for p in got["debug"]["packages"]])
    tagged = [s[0] for s in seen if s]
    return {
        "one_package_each": [len(s) for s in seen],
        "tags_match": tagged == given[-len(seen) :],
        "distinct": len(set(tagged)) == len(tagged),
    }


@runner("dbg.select")
def select(input: dict, ctx: dict):
    """What the stack refuses on the production image: a traced run, and a
    debug load of an image without debug features; the node still serves."""
    card = ctx["session"].dev.card
    out: dict = {}
    try:
        L1.run(card, "memops", {}, reps=1, trace=True)
        out["trace"] = "ran"
    except RuntimeError as e:
        out["trace"] = str(e).split(":")[0]
    try:
        card.load(C.FIRMWARE, debug=True)
        out["load"] = "loaded"
    except abi.AbiMismatch as e:
        out["load"] = str(e).rsplit("(", 1)[-1].rstrip(")")
    if card.debug:
        card.load(debug=False)
    out["state"] = card.q.state()["state"]
    return out


@runner("dbg.late")
def late(input: dict, ctx: dict):
    """The failing package at each bound on the production image: whether
    its wait failed past the bound and within one poll of it, and whether
    the rest of the completion's cycles is the same at every bound."""
    card = ctx["session"].dev.card
    rows = []
    for b in input["bounds"]:
        c, _, text = run_once(card.q, failing(card, ctx["machine"]), b)
        steps = stdout_report(text)["pkgt"].get("steps", 0)
        rows.append((b, steps, c.cycles))
    poll = input["poll"]
    extra = [cy - b for b, _, cy in rows]
    return {
        "never_early": all(s > b for b, s, _ in rows),
        "within_poll": all(s - b <= poll for b, s, _ in rows),
        "fixed_overhead": max(extra) - min(extra) <= poll,
    }


# ----------------------------------------------------------------- mutants
@mutant("dbg/bound_khz")
def m_bound_khz():
    with patched(A, "bound", lambda s, mhz: int(s * mhz * 1e3)):
        yield


@mutant("dbg/no_trace_flag")
def m_no_trace_flag():
    with patched(QL, "RUN_F_TRACE", 0):
        yield


@mutant("dbg/noengine_flag")
def m_noengine_flag():
    with patched(QL, "RUN_F_NOENGINE", 0):
        yield


@mutant("dbg/decode_skips")
def m_decode_skips():
    """A decoder that skips what it cannot parse."""
    keep = C._rs.debug_decode

    def decode(region, machine_json):
        try:
            return keep(region, machine_json)
        except C._rs.Refused:
            return {"packages": []}

    with patched(C._rs, "debug_decode", decode):
        yield


@mutant("dbg/decode_one_package")
def m_decode_one_package():
    """A decoder that runs every submission's records together."""
    keep = C._rs.debug_decode

    def decode(region, machine_json):
        got = keep(region, machine_json)
        ps = got["packages"]
        if ps:
            one = {**ps[0], "steps": [s for p in ps for s in p["steps"]]}
            got = {**got, "packages": [one]}
        return got

    with patched(C._rs, "debug_decode", decode):
        yield


@mutant("dbg/trace_drops_step")
def m_trace_drops_step():
    """A traced run that sends its package without the last step (the
    lowered package the card is handed, whichever stack built it)."""
    keep_run, keep_built = C.NodeCard.run, C.B.built

    def short(*a, **kw):
        return {
            n: (
                plain,
                C.B.Package(
                    C._rs.package_stream(low.raw, "[]", low.machine_json, low.mesh),
                    low.machine_json,
                    low.mesh,
                ),
                b,
            )
            for n, (plain, low, b) in keep_built(*a, **kw).items()
        }

    def run(self, program, *a, trace=False, **kw):
        if not trace:
            return keep_run(self, program, *a, **kw)
        with patched(C.B, "built", short):
            return keep_run(self, program, *a, trace=trace, **kw)

    with patched(C.NodeCard, "run", run):
        yield


@mutant("dbg/no_region")
def m_no_region():
    """The debug image booted without its region."""
    keep = A.BootArgs.pack

    def plain(self):
        saved, self.regions = self.regions, []
        try:
            return keep(self)
        finally:
            self.regions = saved

    with patched(A.BootArgs, "pack", plain):
        yield


@mutant("dbg/names_from_elsewhere")
def m_names_from_elsewhere():
    """Status names not from the machine file: its table reversed."""
    keep = C._rs.debug_decode

    def decode(region, machine_json):
        doc = json.loads(machine_json)
        st = doc["machine"]["fw"]["status"]
        doc["machine"]["fw"]["status"] = dict(
            zip(sorted(st), sorted(st.values(), reverse=True), strict=True)
        )
        return keep(region, json.dumps(doc))

    with patched(C._rs, "debug_decode", decode):
        yield


@mutant("dbg/issued_dropped")
def m_issued_dropped():
    """A decoder that drops the engine's issue count from a failure."""
    keep = C._rs.debug_decode

    def decode(region, machine_json):
        got = keep(region, machine_json)
        for p in got["packages"]:
            if p["fail"]:
                p["fail"]["issued"] = None
        return got

    with patched(C._rs, "debug_decode", decode):
        yield


@mutant("dbg/no_cut")
def m_no_cut():
    """The host's cut ignored: the whole package runs."""
    keep = C.B.built
    with (
        patched(C, "cut", lambda pkg, after: pkg),
        patched(
            C.B,
            "built",
            lambda progs, machine, fetch=None, residents=None, after=None: keep(
                progs, machine, fetch, residents, None
            ),
        ),
    ):
        yield


@mutant("dbg/always_drained")
def m_always_drained():
    """Every failure dump marked stable."""
    with patched(C.NodeCard, "drained", staticmethod(lambda report, console="": True)):
        yield


@mutant("dbg/session_stops")
def m_session_stops():
    """A session that ends at its first failed run."""
    keep = S.DebugSession.attempt

    def attempt(self, fn):
        rec = keep(self, fn)
        if not rec["ok"]:
            raise RuntimeError(rec["error"])
        return rec

    with patched(S.DebugSession, "attempt", attempt):
        yield


@mutant("dbg/restart_after_fail")
def m_restart_after_fail():
    """The host restarts the firmware after every failure."""
    keep = C.NodeCard._debug_rerun

    def rerun(self, *a, **kw):
        self.restart()
        return keep(self, *a, **kw)

    with patched(C.NodeCard, "_debug_rerun", rerun):
        yield


@mutant("dbg/debug_claim")
def m_debug_claim():
    """Every image taken to carry the debug features."""
    keep = abi.check

    def check(elf, version, require=False):
        return {**keep(elf, version, require), "debug": 7, "last": 4}

    with patched(abi, "check", check):
        yield


@mutant("dbg/bound_doubled")
def m_bound_doubled():
    """Each submission's bound written twice as long."""
    keep = QC.NodeQueue.submit

    def submit(self, *a, timeout_cycles=0, **kw):
        return keep(self, *a, timeout_cycles=2 * timeout_cycles, **kw)

    with patched(QC.NodeQueue, "submit", submit):
        yield


__all__ = ["debug_image", "failing", "flags_of"]
