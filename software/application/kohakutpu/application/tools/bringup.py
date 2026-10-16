"""Card bring-up, one hardware step a command, all through the driver.

    python -m kohakutpu.application.tools.bringup STEP [--board B] [--nodes 0,1,2,3]

Steps, in session order (docs/workflow/bringup.md):

    clocks      every node's wizard to a profile (default: the board's idle one)
    read-clocks every node's wizard as it stands, and its lock
    enumerate   read calibration, the write-path preflight, A_CAPS, each node's
                DRAM write path, every unit against the board's
    cpu         cpuprobe on each node through its load window (sysnode CPU)
    boot        the dispatcher on each node, its queue ready, one NOP
    rung NAME   one workload (WORKLOADS) on node --nodes[0], scored
    ladder DOM  one clock domain of one node stepped --start..--stop by --inc,
                the others held at the idle profile; each step re-boots the node
                (a wizard LOAD resets every sysnode), runs the domain's workload
                three times, scores it, and the idle profile is restored after
                every step. Stops at the first hang or transport fault: the link
                is then wedged and only a reprogram clears it.

`--build MODEL` runs any step on a Verilated card instead of JTAG (the same
load-window path), so every step is rehearsed before silicon. Results go to
build/bringup/<board>/ as JSON.
"""

import argparse
import hashlib
import importlib.util
import json
import pathlib
import re
import sys
import time

import numpy as np

from kohakuaccel.driver.device import by_type, rv64load
from kohakuaccel.driver.node.queue import NodeError
from kohakuaccel.driver.transport.jtag import JtagTransport
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakuaccel.driver.transport.simdram import DramMap
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakutpu.application.runtime.card import ARENA, SiliconCard
from kohakutpu.application.tools.l1 import bufs, raw
from kohakutpu.compiler import build
from kohakutpu.driver.clock.card import CardClocks, load_board
from kohakutpu.driver.host import SCRATCH, Card, Mesh, board_map
from kohakutpu.language import examples
from kohakutpu.language.numerics.mxfp7 import value_fp16

ROOT = pathlib.Path(__file__).resolve().parents[5]
OUT = ROOT / "build" / "bringup"
CPUPROBE = ROOT / "software/firmware/tools/cpuprobe.py"


def _script(path: pathlib.Path):
    """A tool script loaded as a module (software/firmware/tools is no package)."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cpuprobe = _script(CPUPROBE)
#: A rehearsal on a Verilated card: its DRAM wraps at 16 MB.
MODEL_ARENA_END = 0x00F0_0000


def f16(rng, *shape):
    return rng.standard_normal(shape).astype(np.float16)


def silu(v):
    v = np.asarray(v, np.float64)
    return v / (1 + np.exp(-v))


# ---- workloads: (example, inputs(rng), reference(inputs) -> {param: want}, exact)
def _copy(rng):
    return {"x": f16(rng, 8, 256)}


def _copy_ref(i):
    return {"y": i["x"].reshape(8, 16, 16).transpose(1, 0, 2).reshape(16, 128)}


def _silu(rng):
    return {"x": f16(rng, 65536)}


def _silu_ref(i):
    return {"y": silu(i["x"])}


def _push(rng):
    return {"x": f16(rng, 1024)}


def _push_ref(i):
    return {"y": i["x"]}


def _mm(rng):
    x = rng.standard_normal((256, 1024)).astype(np.float16)
    w = (rng.standard_normal((256, 1024)) / 32).astype(np.float16)
    return {"x": x, "w": w}


def _mm_ref(i):
    return {"y": value_fp16(i["x"]) @ value_fp16(i["w"]).T}


WORKLOADS = {
    "copy": ("transpose", _copy, _copy_ref, True),
    "vec": ("silu", _silu, _silu_ref, False),
    "noc": ("push", _push, _push_ref, True),
    "mat": ("splitk", _mm, _mm_ref, False),
}
#: The workload that drives each clock domain (a ladder on a domain whose unit
#: sits idle measures nothing). `sys` runs cpuprobe and a dispatch-bound copy.
DOMAIN = {"mat2x": "mat", "vec": "vec", "noc": "noc", "sys": "copy"}


def example_in(space: str, name: str) -> str:
    """Example `name` with its buffers in `space`: "dram" as written; "stage"
    with every `@dram` buffer moved to the running node's staging store, so the
    workload's memory traffic never enters the Xache (written to
    build/bringup/staged/)."""
    if space == "dram":
        return name
    src = examples.example(name).read_text(encoding="utf-8")
    out = OUT / "staged" / f"{name}.ktpu"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(src.replace("@dram", "@stage"), encoding="utf-8")
    return str(out)


def score(out: dict, want: dict) -> dict:
    """Relative error of every output against the reference: p50, p99, max."""
    errs = []
    for k, ref in want.items():
        got = np.asarray(out[k], np.float64).ravel()
        ref = np.asarray(ref, np.float64).ravel()[: got.size]
        scale = max(np.abs(ref).max(), 1e-30)
        errs.append(np.abs(got - ref) / scale)
    e = np.concatenate(errs)
    return {
        "p50": float(np.percentile(e, 50)),
        "p99": float(np.percentile(e, 99)),
        "max": float(e.max()),
    }


def digest(out: dict) -> str:
    h = hashlib.sha1()
    for k in sorted(out):
        h.update(np.ascontiguousarray(out[k]).tobytes())
    return h.hexdigest()[:12]


# ---- the transport
def transport(a):
    """JTAG, or a fresh Verilated card: a fresh model is a freshly programmed
    device, so its wizards go to the idle profile first, as the card's do."""
    if not a.build:
        return JtagTransport()
    t = VerilatorTransport(a.build)
    b = load_board(a.board)
    CardClocks(t, b).set_all(b.get("idle_profile", "low"))
    return t


def idle_of(t, a):
    if a.build:
        return lambda: t.run(600)
    return None


def nodes_of(a) -> list[int]:
    return [int(n) for n in a.nodes.split(",")]


def save(a, name: str, data) -> None:
    d = OUT / a.board
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")


#: Each board's approved bitstream: what `program` loads.
BITSTREAMS = {
    "multimesh_v9t4": r"C:\Users\apoll\Desktop\vivado\multimesh_v9t4"
    r"\multimesh_v9t4.runs\impl_1\multimesh_v9t4_wrapper.bit",
    "multimesh_v9t5": r"C:\Users\apoll\Desktop\vivado\multimesh_v9t5"
    r"\multimesh_v9t5.runs\impl_1\multimesh_v9t5_wrapper.bit",
}


# ---- steps
def step_program(a) -> int:
    """Program the board's approved bitstream, then report every memory
    controller's calibration. The clocks come up at their BUILT rates: run
    `clocks` next, before anything else."""
    if a.build:
        raise SystemExit("program: the card only")
    bit = a.bit or BITSTREAMS[a.board]
    t = JtagTransport()
    t0 = time.monotonic()
    t.program(bit)
    print(f"programmed {bit} in {time.monotonic() - t0:.1f}s", flush=True)
    fails = 0
    for name, props in t.mig_status():
        failed = props.get("CALIBRATION_FAIL.STATUS", "?").upper() != "FALSE"
        stages = [
            k for k, v in props.items() if k.startswith("CAL_STATUS.") and v == "FAIL"
        ]
        print(
            f"{name.rsplit('/', 1)[-1]}: {'FAIL' if failed else 'pass'} "
            f"({props.get('CAL_ERROR_MSG', '?')}){' failed: ' + str(stages) if stages else ''}"
        )
        fails += failed
    return 1 if fails else 0


def step_clocks(a) -> int:
    t = transport(a)
    b = load_board(a.board)
    profile = a.profile or b.get("idle_profile", "low")
    rates = dict(b["profiles"][profile])
    for kv in a.set:
        dom, mhz = kv.split("=")
        rates[dom] = float(mhz)
    clocks = CardClocks(t, b)
    for n in nodes_of(a):
        got = clocks.mesh(n).set_profile(rates)
        print(
            f"node {n}: {profile} {a.set or ''} {got} locked={clocks.mesh(n).locked()}"
        )
    return 0


def step_read_clocks(a) -> int:
    t = transport(a)
    clocks = CardClocks(t, load_board(a.board))
    for n in nodes_of(a):
        m = clocks.mesh(n)
        print(f"node {n}: {m.read_all()} locked={m.locked()}")
    return 0


def step_enumerate(a) -> int:
    t = transport(a)
    b = load_board(a.board)
    card = Card.from_board(a.board, transport=t, which=nodes_of(a), reply_at_agent=True)
    card.verify_all()
    fails = 0
    want = b.get("units", {})
    for mesh in card.meshes:
        got = {k: sorted(mesh.coords(k)) for k in ("MG", "VC")}
        ok = all(got[k] == sorted(tuple(p) for p in want.get(k, [])) for k in got)
        fails += not ok
        print(f"node {mesh.index}: caps {mesh.caps}; {got} {'ok' if ok else 'DIFFERS'}")
    save(a, "enumerate", {m.index: repr(m) for m in card.meshes})
    return 1 if fails else 0


def cpu(a, t, nodes) -> int:
    """cpuprobe on each of `nodes` over `t`; the count that failed. Its region
    is DRAM (it tests the node's Xache path), so it refuses `--space stage`."""
    if a.space == "stage":
        raise SystemExit("cpuprobe runs in DRAM through the Xache: --space dram")
    m = board_map(load_board(a.board))
    mem = UnitGlobal(t, m["dram"], m["mem"], m["size"], staging=True)
    idle = idle_of(t, a) or (lambda: time.sleep(0.05))
    fails = 0
    for n in nodes:
        ok, r = cpuprobe.card_run(t, m, n, mem, a.rounds, a.seed_cpu, idle, 600.0)
        print(
            f"cpuprobe node {n}: {'ok' if ok else 'FAIL'} {r['cycles']} cycles "
            f"({r['wall']:.1f}s)" + (f"; MISMATCH {r['bad']}" if r["bad"] else "")
        )
        fails += not ok
    return fails


def home_line(board: dict, home: int) -> int:
    """A 32-byte line in the driver's scratch megabyte whose Xache home is
    `home`: the interleave (16 KB granules) puts consecutive granules on
    different homes, so the first granule of the scratch that maps there."""
    dm = DramMap.of_board(board, board["meshes"])
    for k in range(64):
        off = SCRATCH + k * (16 << 10)
        if dm.locate(off)[0] == home:
            return off
    raise ValueError(f"no scratch granule on home {home}")


#: A staging line past the node queue's megabyte (node-local memory, no Xache).
STAGE_LINE = 0x1F_F000


def step_info(a) -> int:
    """Per node, control path only: A_CAPS, every unit's CU_CAPS through the
    node's mailbox, and the RV64 processor's status. No memory access."""
    b = load_board(a.board)
    m = board_map(b)
    t = transport(a)
    fails = 0
    for n in nodes_of(a):
        mesh = Mesh(
            t,
            n,
            m["ctrl"][n],
            m["mem"][n],
            m["size"],
            dram_base=m["dram"][n],
            granule=m["granule"],
            reply_at_agent=True,
        )
        units = {
            k: sorted(tuple(c) for c in v) for k, v in by_type(mesh.endpoints).items()
        }
        want = {k: sorted(tuple(p) for p in v) for k, v in b.get("units", {}).items()}
        ok = all(units.get(k) == v for k, v in want.items())
        st = rv64load.LoadWindow(t, m["ctrl"][n] + rv64load.WINDOW_OFFSET).status()
        print(
            f"node {n}: {mesh.caps}; units {units} {'= board' if ok else 'DIFFER'}; "
            f"cpu status {st:#x}",
            flush=True,
        )
        fails += not ok
    return 1 if fails else 0


def step_stage(a) -> int:
    """One 32-byte line in each node's own staging store, written and read back
    through that node's host window: node-local memory, no die crossing. (A
    window never reaches another mesh's store: such an address goes to its own
    DRAM past the fitted memory and the access never completes.)"""
    m = board_map(load_board(a.board))
    t = transport(a)
    fails = 0
    for n in nodes_of(a):
        at = m["mem"][n] | 1 << 39 | n << 36 | STAGE_LINE
        want = bytes((0x3C ^ (n << 5) ^ i) & 0xFF for i in range(32))
        if a.read:
            print(f"node {n}: staging {at:#x} read", flush=True)
            got = t.read_block(at, 32)
            tag = "pattern" if got == want else "other"
            print(f"node {n}: {got.hex()} ({tag})", flush=True)
            continue
        print(f"node {n}: staging {at:#x} write", flush=True)
        t.write_block(at, want)
        got = t.read_block(at, 32)
        ok = got == want
        print(f"node {n}: read back {'OK' if ok else 'WRONG ' + got.hex()}", flush=True)
        fails += not ok
    return 1 if fails else 0


def step_line(a) -> int:
    """`--bytes` (32) at the start of a 128-byte line of home --home, through
    node --nodes[0]'s host window. `--op wr` (default) writes then reads back;
    `w` is the write alone (one AXI write: 32 B is one partially-strobed beat
    of the 512-bit path, 64 B one full beat); `r` the read alone (single-beat
    8-byte reads), checked against the pattern node `--writer` (default: the
    reading node) wrote, so one node's write can be read through another's."""
    b = load_board(a.board)
    m = board_map(b)
    node, home = nodes_of(a)[0], a.home
    writer = node if a.writer is None else a.writer
    off = home_line(b, home) + a.at
    if a.at % 128 or a.at >= 16 << 10:
        raise SystemExit("line: --at is a 128-byte step inside the 16 KB granule")
    at = m["mem"][node] + off
    want = bytes(((writer << 4) ^ (home << 2) ^ i) & 0xFF for i in range(a.bytes))
    t = transport(a)
    if "w" in a.op:
        mine = bytes(((node << 4) ^ (home << 2) ^ i) & 0xFF for i in range(a.bytes))
        print(
            f"via node {node} -> home {home}: write {a.bytes} B at {at:#x} "
            f"(flat {off:#x})",
            flush=True,
        )
        t.write_block(at, mine)
        print("write done (response received)", flush=True)
    if "r" not in a.op:
        return 0
    print(f"via node {node} -> home {home}: read {a.bytes} B at {at:#x}", flush=True)
    got = t.read_block(at, a.bytes)
    ok = got == want
    print(f"read back {'OK' if ok else 'WRONG'} (pattern of node {writer})")
    if not ok:
        for k in range(0, a.bytes, 8):
            print(f"  +{k:#04x} got {got[k:k + 8].hex()} want {want[k:k + 8].hex()}")
    return 0 if ok else 1


def example_with(name: str, spaces: dict) -> str:
    """Example `name` with parameter p's buffer in ``spaces[p]`` ("stage" or
    "dram"), the rest as written (build/bringup/spaced/)."""
    src = examples.example(name).read_text(encoding="utf-8")
    for p, space in spaces.items():
        src = re.sub(rf"(\b{p}:\s*[^,)@]+@)(dram|stage)", rf"\g<1>{space}", src)
    tag = "_".join(f"{p}{s[0]}" for p, s in sorted(spaces.items()))
    out = OUT / "spaced" / f"{name}_{tag}.ktpu"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(src, encoding="utf-8")
    return str(out)


def step_xmove(a) -> int:
    """Node --nodes[0]'s mover runs the copy workload between its own staging
    and DRAM on home --home: `--dir out` copies staging -> DRAM (the mover's
    writes cross to the home), `--dir in` DRAM -> staging (its reads cross).

    The host never touches the crossing: it fills and checks the DRAM side
    through home --home's own node window (in-die) and the staging side
    through the node's own window, so a lost transfer stalls the node's
    mover, not the JTAG link. On a timeout the DRAM side is read anyway: what
    landed separates a request that never arrived from a response that never
    came back."""
    b = load_board(a.board)
    m = board_map(b)
    node, home = nodes_of(a)[0], a.home
    off = home_line(b, home)
    t = transport(a)
    card = open_card(a, t, [node])
    card.timeout = a.wait
    rng = np.random.default_rng(a.seed)
    x = _copy(rng)["x"]
    want = _copy_ref({"x": x})["y"]
    dram_host = m["mem"][home] + off
    dram_unit = m["dram"][node] + off
    if a.dir == "out":
        src = example_with("transpose", {"x": "stage", "y": "dram"})
        xs = card.stage(x.nbytes)
        card.write(xs, x.tobytes())
        t.write_block(dram_host, bytes([POISON]) * want.nbytes)
        binds = {"x": xs, "y": dram_unit}
    else:
        src = example_with("transpose", {"x": "dram", "y": "stage"})
        t.write_block(dram_host, x.tobytes())
        ys = card.stage(want.nbytes)
        card.write(ys, bytes([POISON]) * want.nbytes)
        binds = {"x": dram_unit, "y": ys}
    if a.probe:
        poff = home_line(b, node) + 0x2000
        pat = bytes(((node << 4) ^ (node << 2) ^ i) & 0xFF for i in range(32))
        t.write_block(m["mem"][node] + poff, pat)
    mod = examples.load(examples.example(src))
    fn = mod.names()[0]
    progs = build.programs(mod, fn, binds, card.machine.for_mesh(node))
    print(
        f"node {node} mover, {a.dir}: DRAM {dram_unit:#x} on home {home} "
        f"(checked via node {home}'s window {dram_host:#x})",
        flush=True,
    )
    hung = None
    try:
        res = card.run(progs[None])
        print(f"ran: {res['cycles']} cycles", flush=True)
    except (TimeoutError, NodeError) as e:
        hung = str(e)[:300]
        print(f"NO COMPLETION after {a.wait}s: {hung}", flush=True)
    if a.dir == "out":
        got = np.frombuffer(t.read_block(dram_host, want.nbytes), np.float16)
    else:
        got = np.frombuffer(card.get(binds["y"], want.nbytes), np.float16)
    flat = want.ravel()
    same = got.view(np.uint16) == flat.view(np.uint16)
    poison = got.view(np.uint16) == (POISON << 8 | POISON)
    print(
        f"destination: {int(same.sum())} of {flat.size} values right, "
        f"{int(poison.sum())} still poison, {int((~same & ~poison).sum())} other"
    )
    if a.probe:
        # A line of the node's OWN home, planted before the run: what the node
        # reads back after it shows whether a stray response reached the node.
        poff = home_line(b, node) + 0x2000
        pat = bytes(((node << 4) ^ (node << 2) ^ i) & 0xFF for i in range(32))
        got_p = t.read_block(m["mem"][node] + poff, 32)
        print(f"probe own-home line: {'node pattern' if got_p == pat else got_p.hex()}")
    if hung is None:
        card.close()
    else:
        print("node left stalled (its queue is not stopped); reprogram to clear")
    return 0 if hung is None and same.all() else 1


def step_cpu(a) -> int:
    return 1 if cpu(a, transport(a), nodes_of(a)) else 0


def open_card(a, t, nodes, profile: str | None = "idle"):
    """The card on `nodes`. Under `--space stage` the DRAM arena is empty, so a
    DRAM placement raises instead of reaching the Xache."""
    kw = {"arena_end": MODEL_ARENA_END} if a.build else {}
    if a.space == "stage":
        kw["arena_end"] = ARENA
    return SiliconCard(
        a.board,
        transport=t,
        nodes=tuple(nodes),
        profile=profile,
        idle=idle_of(t, a),
        **kw,
    )


def step_boot(a) -> int:
    t = transport(a)
    t0 = time.monotonic()
    card = open_card(a, t, nodes_of(a))
    booted = time.monotonic() - t0
    for n, q in card.queues.items():
        c = q.nop()
        print(f"node {n}: ready, NOP {c.describe()}")
    print(f"booted {len(card.queues)} node(s) in {booted:.1f}s")
    card.close()
    return 0


#: The byte every output is painted with before a run: an output a unit never
#: wrote reads back as this, never as an earlier run's result.
POISON = 0xA5


def rung(card, name: str, seed: int, reps: int = 1, space: str = "dram") -> dict:
    """Workload `name` `reps` times on `card` with its buffers in `space`:
    scores, digests, cycles. The inputs cross the host link once; each run
    paints the outputs with POISON, runs, and reads back the outputs only."""
    example, inputs, ref, exact = WORKLOADS[name]
    rng = np.random.default_rng(seed)
    ins = inputs(rng)
    want = ref(ins)
    m = examples.load(examples.example(example_in(space, example)))
    fn = m.names()[0]
    held = card.mark()
    try:
        binds, layout = {}, bufs(m, fn)
        for p, b in layout:
            node = b.layout.get("node")
            if p in want:
                binds[p] = card.place(b.nbytes, b.space, node)
                continue
            data = raw(b, ins[p]) if p in ins else bytes(b.nbytes)
            binds[p] = card.put(np.frombuffer(data, np.uint8), b.space, node)
        # A body with no node block compiles against the machine's default mesh:
        # seen from the node it runs on, its units are that node's.
        progs = build.programs(m, fn, binds, card.machine.for_mesh(card.nodes[0]))
        prog = progs[None] if list(progs) == [None] else progs
        outs = [(p, b) for p, b in layout if p in want]
        runs = []
        for _ in range(reps):
            for p, b in outs:
                card.write(binds[p], bytes([POISON]) * b.nbytes)
            res = card.run(prog)
            out = {p: b.unpack(card.get(binds[p], b.nbytes)) for p, b in outs}
            s = score(out, want)
            runs.append({"cycles": res["cycles"], "digest": digest(out), **s})
    finally:
        card.release(held)
    same = len({r["digest"] for r in runs}) == 1
    ok = same and (all(r["max"] == 0.0 for r in runs) if exact else True)
    return {
        "workload": name,
        "example": example,
        "space": space,
        "exact": exact,
        "same": same,
        "ok": ok,
        "runs": runs,
    }


def step_rung(a) -> int:
    t = transport(a)
    node = nodes_of(a)[0]
    card = open_card(a, t, [node])
    try:
        r = rung(card, a.what, a.seed, reps=a.reps, space=a.space)
    finally:
        card.close()
    save(a, f"rung_{a.what}_n{node}", r)
    for x in r["runs"]:
        print(f"{a.what} ({r['example']}): {x}")
    print(f"identical across runs: {r['same']}; {'ok' if r['ok'] else 'CHECK'}")
    return 0 if r["ok"] else 1


def step_ladder(a) -> int:
    dom = a.what
    if dom not in DOMAIN:
        raise SystemExit(f"ladder: domain {dom!r} is one of {sorted(DOMAIN)}")
    node = nodes_of(a)[0]
    b = load_board(a.board)
    low = b["profiles"][b.get("idle_profile", "low")]
    others = {k: v for k, v in low.items() if k != dom}
    t = transport(a)
    clocks = CardClocks(t, b).mesh(node)
    rows = []
    mhz = a.start
    try:
        while mhz <= a.stop + 1e-9:
            rates = clocks.set_isolated(dom, mhz, others)
            row = {"mhz": mhz, "rates": rates}
            try:
                card = open_card(a, t, [node], profile=None)
                row.update(rung(card, DOMAIN[dom], a.seed, reps=3, space=a.space))
                card.stop()
                if dom == "sys" and a.space == "dram":
                    bad = cpu(a, t, [node])
                    row["cpuprobe"] = not bad
                    row["ok"] = row["ok"] and not bad
            except (TimeoutError, NodeError) as e:
                row.update({"ok": False, "hang": str(e)[:300]})
            rows.append(row)
            best = row.get("runs", [{}])[-1]
            print(
                f"{dom} {mhz:7.2f} MHz: {'ok' if row['ok'] else 'FAIL'} "
                f"p50 {best.get('p50', '-')} p99 {best.get('p99', '-')} "
                f"max {best.get('max', '-')} same {row.get('same', '-')}"
                + (f" HANG {row['hang']}" if "hang" in row else ""),
                flush=True,
            )
            if "hang" in row:
                break
            clocks.set_profile(low)
            mhz += a.inc
    finally:
        save(a, f"ladder_{dom}_n{node}", rows)
        if not rows or "hang" not in rows[-1]:
            clocks.set_profile(low)
            print(f"node {node} back at {clocks.read_all()}")
        else:
            print(
                "stopped on a hang: the link may be wedged -- reprogram, then "
                "`bringup clocks` before anything else"
            )
    return 0


STEPS = {
    "program": step_program,
    "clocks": step_clocks,
    "read-clocks": step_read_clocks,
    "enumerate": step_enumerate,
    "cpu": step_cpu,
    "boot": step_boot,
    "rung": step_rung,
    "ladder": step_ladder,
    "line": step_line,
    "xmove": step_xmove,
    "info": step_info,
    "stage": step_stage,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=sorted(STEPS))
    ap.add_argument(
        "what", nargs="?", default="", help="rung: a workload; ladder: a domain"
    )
    ap.add_argument("--board", default="multimesh_v9t4")
    ap.add_argument("--nodes", default="0")
    ap.add_argument("--home", type=int, default=0, help="line: the Xache home")
    ap.add_argument("--read", action="store_true", help="stage: read only, no write")
    ap.add_argument(
        "--bit", default=None, help="program: a bitstream other than the board's"
    )
    ap.add_argument(
        "--op", choices=("wr", "w", "r"), default="wr", help="line: write, read, both"
    )
    ap.add_argument(
        "--bytes", type=int, choices=(32, 64, 128), default=32, help="line: size"
    )
    ap.add_argument(
        "--writer", type=int, default=None, help="line: whose pattern a read expects"
    )
    ap.add_argument(
        "--at", type=lambda s: int(s, 0), default=0, help="line: offset in the granule"
    )
    ap.add_argument(
        "--dir",
        choices=("out", "in"),
        default="out",
        help="xmove: staging->DRAM or back",
    )
    ap.add_argument(
        "--wait",
        type=float,
        default=30.0,
        help="xmove: seconds before a stall is called",
    )
    ap.add_argument(
        "--probe",
        action="store_true",
        help="xmove: plant a line in the node's own home, read it after the run",
    )
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        help="clocks: DOM=MHZ over the profile, e.g. --set sys=10",
    )
    ap.add_argument(
        "--space",
        choices=("stage", "dram"),
        default="stage",
        help="rung/ladder buffers: the node's staging (no Xache) or DRAM",
    )
    ap.add_argument("--build", default=None, help="a Verilated card instead of JTAG")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--seed-cpu", type=lambda s: int(s, 0), default=None, help="cpuprobe's seed"
    )
    ap.add_argument("--start", type=float, default=100.0)
    ap.add_argument("--stop", type=float, default=300.0)
    ap.add_argument("--inc", type=float, default=25.0, help="ladder: MHz a step")
    a = ap.parse_args()
    if a.seed_cpu is None:
        a.seed_cpu = cpuprobe.SEED
    return STEPS[a.step](a)


if __name__ == "__main__":
    sys.exit(main())
