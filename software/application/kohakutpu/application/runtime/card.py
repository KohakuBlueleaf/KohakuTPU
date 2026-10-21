"""Node programs on a card: boot once, place arrays, run, time, read back.

    card = VerilatorCard(build)                         # the Verilated card
    card = SiliconCard("multimesh_v9t4", nodes=(0,))    # the card, over JTAG
    card = SiliconCard("multimesh_v9t6", transport="xdma")  # over PCIe (Linux)
    a = card.put(np.float16 array)      # unit-global byte address
    got = card.run(program)             # node cycles, per-step ends, phases
    card.get(a, nbytes)
    card.close()

    got = card.run({0: p0, 1: p1})      # one package a node, all submitted first

`NodeCard` is the one runtime; the two cards differ only in what they are
handed: the transport, how the host reaches DRAM, how a poll idles, how a
firmware image is placed, and the arena. The machine programs are built for
comes from the board (`target.machine_of`) and is checked against the
enumerated units at boot.

Before a set of programs first runs, the static checker runs on it
(`check="refuse"`: a finding of a `verify.REFUSE` kind raises
`verify.Refused`; "warn" prints the findings and runs; "off").

`cycles` is the package's own count (node clock); with `steps=True` the
firmware prints each step's end (KA_BOOT_F_STEPS), prints excluded. The nodes
share DRAM (the Xache); each node's queue sits in its own staging store.
"""

import json
import os
import pathlib
import re
import sys
import tempfile
import time

import numpy as np

from kohakuaccel.compiler.machine import STAGE_BYTES
from kohakuaccel.driver.node.boot import BootArgs, NodeBoot
from kohakuaccel.driver.node.queue import NodeError, NodeQueue
from kohakuaccel.driver.transport.jtag import JtagTransport
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakuaccel.driver.transport.simdram import SimDram
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakuaccel.driver.transport.xdma_linux import XdmaLinux
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.compiler import build as B
from kohakutpu.compiler import imem, verify
from kohakutpu.compiler import target as T
from kohakutpu.driver import units as _units  # noqa: F401  (names MG and VC)
from kohakutpu.driver.clock.card import BOARDS, CardClocks, load_board
from kohakutpu.driver.host import ARENA_LIMIT, Card, board_map

ROOT = pathlib.Path(__file__).resolve().parents[5]
FIRMWARE = ROOT / "build/fw/kohakutpu_node.elf"
#: The checker's findings by `verify.key`: a program set seen before is not
#: checked again.
VERIFY_CACHE = ROOT / "build" / "verify"
#: Host arrays start past the node queue's alias at 0.
ARENA = 0x0010_0000
#: The node queue, at the start of each node's staging store.
Q_SIZE = 0x0010_0000
#: Each node queue's submission and completion ring depth: a replayed step
#: (`NodeCard.replay`) submits its packages back to back.
RING = 64
F_TIMING, F_STEPS = 2, 8
#: Boot flags OR'ed into every node's (KA_BOOT_F_*: 1 serial, 4 no fetch,
#: 16 no dispatch engine).
FLAGS_ENV = "KOHAKUTPU_BOOT_FLAGS"
#: "0": every package carries its vector images whole (no residency).
RESIDENT_ENV = "KOHAKUTPU_RESIDENT"
#: A directory: a package that fails is kept there (bytes and bindings).
FAILED_ENV = "KOHAKUTPU_KEEP_FAILED"


def node_queue(mem, base: int, memory: CardMemory, **kw) -> NodeQueue:
    """A node's queue at `base`: a package too large for its heap takes its
    place from `memory`, the card's one owner of DRAM, and gives it back
    there when dropped."""
    return NodeQueue(
        mem,
        base,
        size=Q_SIZE,
        sq_n=RING,
        cq_n=RING,
        spill=memory.alloc,
        unspill=memory.free,
        **kw,
    )


class CardMismatch(RuntimeError):
    """The card's enumerated units are not the machine programs are built for."""


#: The unit kinds a KohakuTPU mesh holds (matmul clusters, vector cores,
#: Streaming Row Units).
KINDS = ("MG", "VC", "SR")


def enumerated(meshes) -> dict:
    """``{kind: [coord, ..]}`` of the units every one of `meshes` answered
    with; meshes that differ raise :class:`CardMismatch` (the machine is one
    die repeated)."""
    found = [{k: sorted(m.coords(k)) for k in KINDS} for m in meshes]
    if any(f != found[0] for f in found):
        raise CardMismatch(f"the card's meshes hold different units: {found}")
    return found[0]


class ClockMismatch(RuntimeError):
    """A node's wizard is not at the profile the card was opened for."""


class NodeCard:
    """The runtime over one card: `t` the transport, `host` the DRAM path the
    host writes through, `idle` what a queue poll does between reads, `boot`
    the firmware placement ("axi" through the load window, "burn" a model's
    arrays), arrays in [ARENA, `arena_end`)."""

    def __init__(
        self,
        t,
        host,
        board: str,
        *,
        nodes: tuple[int, ...] = (0,),
        fw=None,
        steps=False,
        machine=None,
        timeout: float = 300.0,
        check: str = "refuse",
        idle=None,
        boot: str = "axi",
        arena_end: int = ARENA_LIMIT,
        spin: float = 0.0,
        sys_hz: float | None = None,
    ) -> None:
        if check not in verify.GATES:
            raise ValueError(f"check={check!r}: one of {verify.GATES}")
        self.t, self.host = t, host
        #: The events since `record`, None while not recording.
        self.log: list | None = None
        self.nodes: tuple[int, ...] = tuple(nodes)
        self.check = check
        #: (node, id(program)) pairs -> (the programs, their findings)
        self.checked: dict = {}
        #: Wall seconds a package may take before its wait raises.
        self.timeout = timeout
        self.board = load_board(board)
        mp = board_map(self.board)
        span = max(self.nodes) + 1
        #: The memory port a fetched dispatch reads through, from the board.
        b = self.board
        self.fetch = tuple(b["fetch_port"]) if "fetch_port" in b else None
        self.arena_end = arena_end
        card = Card.from_board(
            board,
            transport=host,
            which=list(self.nodes),
            verify=False,
            reply_at_agent=True,
        )
        # Programs are built for the units the card enumerates; the board
        # gives the address map, clocks and ports.
        self.machine = machine or T.machine_of(
            {**self.board, "units": enumerated(card.meshes)},
            nodes=span if span > 1 else None,
        )
        for mesh in card.meshes:
            self.check_units(mesh)
        self.mem = UnitGlobal(host, mp["dram"], mp["mem"], mp["size"], staging=True)
        #: The host's whole write: a DRAM write is padded to it. Where the
        #: host's reads are wider than the memory endpoint's write granule
        #: (XDMA lines), every line a read may cover is painted first
        #: (`paint`): never-written ECC DRAM answers with an error.
        wide = getattr(host, "line", 0)
        self.line = max(mp["granule"] or 32, wide)
        self.paint = wide > (mp["granule"] or 32)
        #: The one owner of the card's DRAM arrays and every node's staging
        #: store past its queue: arrays, scratch and spilled packages alike.
        self.memory = CardMemory(
            (ARENA, arena_end),
            {
                m.index: (
                    self.machine.stage_addr(0, mesh=m.index) + Q_SIZE,
                    self.machine.stage_addr(0, mesh=m.index) + STAGE_BYTES,
                )
                for m in card.meshes
            },
            paint=self.mem.write_block if self.paint else None,
        )
        flags = (
            F_TIMING
            | (F_STEPS if steps else 0)
            | int(os.environ.get(FLAGS_ENV, "0"), 0)
        )
        fw = pathlib.Path(fw or FIRMWARE)
        #: node -> its queue, in its own staging store
        self.queues = {}
        #: node -> (its loader, its boot block), for :meth:`restart`
        self.boots = {}
        self.fw = fw
        for mesh in card.meshes:
            i = mesh.index
            sim = t if boot == "burn" else None
            loader = NodeBoot(t, mp["ctrl"][i], i, sim=sim)
            # A firmware left running (a session that never sent STOP) still
            # writes its queue: stop the core before the header is rewritten.
            loader.win.stop()
            q = node_queue(
                self.mem,
                self.machine.stage_addr(0, mesh=i),
                self.memory,
                idle=idle,
                spin=spin,
            )
            q.init()
            args = BootArgs(
                queue=q.base, mesh=i, scan=mesh.caps.grid_hi + 1, flags=flags
            )
            if sys_hz:
                # The firmware gives up when the host would: a vector RUN
                # retires only at its end, so a long one trips nothing first.
                args.timeout = int(self.timeout * sys_hz)
            loader.load(fw, args, boot)
            self.queues[i] = q
            self.boots[i] = (loader, args)
        for q in self.queues.values():
            q.wait_ready()
        self.q = self.queues[self.nodes[0]]
        self.runs = 0
        #: Each node's vector cores' instruction memory and descriptors.
        self.residents = {i: imem.Cores(imem.IMEM_WORDS) for i in self.queues}
        self.resident = self.residents[self.nodes[0]]

    def restart(self) -> None:
        """Every node's firmware started again from the image its instruction
        memory still holds, after a reset that kept the arrays (a wizard
        relock resets every sysnode and mesh; DRAM, staging and the placed
        buffers survive). Queues re-initialised; the vector cores' resident
        images are forgotten, so the next run installs them again."""
        for i, (loader, args) in self.boots.items():
            loader.win.stop()
            self.queues[i].init()
            loader.restart(self.fw, args)
        for q in self.queues.values():
            q.wait_ready()
        self.residents = {i: imem.Cores(imem.IMEM_WORDS) for i in self.queues}
        self.resident = self.residents[self.nodes[0]]

    def check_units(self, mesh) -> None:
        """Raise :class:`CardMismatch` unless the mesh's units are the machine's.
        Programs name units by coordinate, so the scan order does not matter."""
        for kind, want in self.machine.mesh(mesh.index).units.items():
            got = mesh.coords(kind)
            if sorted(got) != sorted(want):
                raise CardMismatch(
                    f"mesh {mesh.index}'s {kind} units are {list(got)}, the "
                    f"machine's {list(want)}: a program built for one runs wrong "
                    f"on the other"
                )

    # ---------------------------------------------------------- memory
    def alloc(self, nbytes: int) -> int:
        return self.memory.alloc(nbytes)

    def free(self, addr: int) -> None:
        self.memory.free(addr)

    def mark(self) -> tuple:
        """What is held now; `release` frees everything placed since."""
        return self.memory.mark()

    def release(self, mark: tuple) -> None:
        self.memory.release(mark)

    def drop_spilled(self) -> None:
        """Every node's packages placed outside its queue's heap given back."""
        for q in self.queues.values():
            q.drop_spilled()

    def stage(self, nbytes: int, node: int | None = None) -> int:
        """`nbytes` of a node's MAG staging store (the card's first by
        default), past its queue."""
        return self.memory.stage(nbytes, self.nodes[0] if node is None else node)

    def place(self, nbytes: int, space: str = "dram", node: int | None = None) -> int:
        """`nbytes` in DRAM or, for ``space == "stage"``, in a node's staging."""
        if space == "stage":
            return self.stage(nbytes, node=node)
        return self.alloc(nbytes)

    def put(self, array, space: str = "dram", node: int | None = None) -> int:
        raw = np.ascontiguousarray(array).tobytes()
        at = self.place(len(raw), space, node)
        self.mem.write_block(at, raw)
        return at

    def write(self, at: int, raw: bytes) -> None:
        if self.log is not None:
            self.log.append(("write", at, bytes(raw)))
        self.mem.write_block(at, raw)

    def get(self, at: int, nbytes: int) -> bytes:
        if self.log is not None:
            self.log.append(("read", at, nbytes))
        return self.mem.read_block(at, nbytes)

    # ------------------------------------------------------ record / replay
    def record(self) -> None:
        """Log every host write, host read and run's packages from now on."""
        self.log = []

    def recorded(self) -> list:
        """The log since `record`; logging off."""
        log, self.log = self.log or [], None
        return log

    def replay(self, log: list) -> list:
        """Run a `record`ed log again: each write rewritten, each run's
        packages (already in the nodes' heaps) submitted with its bindings.
        Runs on one node go back to back -- the node runs its queue in
        order -- and the host waits only where the next event needs the
        card idle: a write, or a run on another set of nodes. Returns each
        run's node cycles. Raises :class:`ValueError` for a log that reads
        (its later events may depend on what was read)."""
        if any(ev[0] == "read" for ev in log):
            raise ValueError("a log that reads the card cannot be replayed")
        cycles: list = []
        pending: dict = {}
        last = None
        for ev in log:
            if ev[0] == "write":
                self._drain(pending, cycles)
                self.mem.write_block(ev[1], ev[2])
                continue
            nodes = tuple(sorted(ev[1]))
            if len(nodes) > 1 or nodes != last:
                self._drain(pending, cycles)
            for n, (raw, bindings) in ev[1].items():
                pending.setdefault(n, []).append(
                    self.queues[n].submit(raw, list(bindings))
                )
            last = nodes
        self._drain(pending, cycles)
        self.runs += sum(1 for ev in log if ev[0] == "run")
        return cycles

    def _drain(self, pending: dict, cycles: list) -> None:
        for n, tags in pending.items():
            for tag in tags:
                cycles.append(self.queues[n].wait(tag, timeout=self.timeout).cycles)
        pending.clear()

    def _keep_failed(self, pkg, bindings) -> None:
        where = os.environ.get(FAILED_ENV)
        if not where:
            return
        d = pathlib.Path(where)
        d.mkdir(parents=True, exist_ok=True)
        stem = d / f"failed_{self.runs:05d}"
        stem.with_suffix(".pkg").write_bytes(pkg.to_bytes())
        stem.with_suffix(".json").write_text(json.dumps([int(b) for b in bindings]))
        print(f"kept the failed package at {stem}.pkg", file=sys.stderr, flush=True)

    # ------------------------------------------------------------- run
    def run(self, program, faults: bool = False) -> dict:
        """Run `program` once: ``{"cycles", "steps", "phases", "pkg"}``. Given
        ``{node: Program}`` (None for this card's first node), every node's
        package is submitted before any is waited on; the result is per node
        under ``"nodes"`` and ``"cycles"`` is the longest. With `faults`, a
        node that times out or faults reports ``{"fault": text}`` and the
        other nodes are still waited for."""
        single = not isinstance(program, dict)
        progs = {self.nodes[0]: program} if single else program
        progs = {self.nodes[0] if n is None else n: p for n, p in progs.items()}
        self.verify(progs)
        if os.environ.get(RESIDENT_ENV) == "0":
            for cores in self.residents.values():
                for r in cores.values():
                    r.forget()
        built = B.packages(progs, self.machine, self.fetch, self.residents)
        if self.log is not None:
            self.log.append(
                (
                    "run",
                    {n: (pkg.to_bytes(), tuple(b)) for n, (pkg, b) in built.items()},
                )
            )
        if single:
            pkg, bindings = built[self.nodes[0]]
            try:
                done = self.q.run(pkg.to_bytes(), bindings, timeout=self.timeout)
            except NodeError:
                self._keep_failed(pkg, bindings)
                raise
            self.runs += 1
            return self._result(self.q, done, pkg)
        tags = {}
        for n, (pkg, bindings) in built.items():
            tags[n] = (self.queues[n].submit(pkg.to_bytes(), bindings), pkg)
        out = {}
        for n, (tag, pkg) in tags.items():
            try:
                done = self.queues[n].wait(tag, timeout=self.timeout)
            except (TimeoutError, NodeError) as e:
                if not faults:
                    raise
                out[n] = {"fault": str(e)}
                continue
            try:
                out[n] = self._result(self.queues[n], done, pkg)
            except ValueError as e:
                # The node's own report is unreadable: its processor misbehaved.
                if not faults:
                    raise
                out[n] = {"fault": f"garbled node report: {e}"}
        self.runs += 1
        cycles = [r["cycles"] for r in out.values() if "cycles" in r]
        return {"cycles": max(cycles, default=0), "nodes": out}

    def verify(self, progs: dict) -> list:
        """The checker's findings for `progs` (``{node: Program}``), once per
        set of programs: raises `verify.Refused` under ``check="refuse"``."""
        key = tuple(sorted((n, id(p)) for n, p in progs.items()))
        if key not in self.checked:
            found = verify.gate(
                progs, self.machine, self.fetch, self.check, cache=VERIFY_CACHE
            )
            # The programs are held so their ids are not reused.
            self.checked[key] = (tuple(progs.values()), found)
            for f in found:
                print(f"verify: {f}", file=sys.stderr, flush=True)
        return self.checked[key][1]

    def _result(self, q, done, pkg) -> dict:
        steps, phases = [], {}
        for ln in q.read_stdout().splitlines():
            if ln.startswith("PKGT "):
                words = ln.split()[1:]
                phases = {
                    words[i]: int(words[i + 1]) for i in range(0, len(words) - 1, 2)
                }
            if ln.startswith("PKGS "):
                first, last, at = (int(v) for v in ln.split()[1:4])
                steps = [] if first == 0 else steps
                what = [
                    f"{s.op.name} u{s.unit} n{s.count}"
                    for s in pkg.steps[first : last + 1]
                ]
                steps.append((first, last, at, ", ".join(what)))
        return {"cycles": done.cycles, "steps": steps, "phases": phases, "pkg": pkg}

    def stop(self) -> None:
        """Ask every node's firmware to leave its loop."""
        for q in self.queues.values():
            q.stop()


class VerilatorCard(NodeCard):
    """The Verilated card: host DRAM traffic through the model's arrays
    (`SimDram`), polls advance the model, firmware burnt into its memories."""

    def __init__(
        self, build, board: str = "multimesh_v9", log=None, args=(), **kw
    ) -> None:
        self.build = pathlib.Path(build)
        # A log of its own: cards run in parallel on one build.
        log = log or pathlib.Path(tempfile.mkdtemp(prefix="ktpu_card_")) / "model.log"
        # `args`: harness flags (sim/verilator/docs/card-backend.md).
        t = VerilatorTransport(self.build, log=log, args=tuple(args))
        b = load_board(board)
        mp = board_map(b)
        host = SimDram(t, b, mp["mem"], mp["size"])
        # 1 MB short of the model's DRAM (it wraps), and below the node range.
        dram = min(len(host.dram) * host.words * 64, 0x1000_0000)
        super().__init__(
            t,
            host,
            board,
            idle=lambda: t.run(600),
            boot="burn",
            arena_end=dram - 0x0010_0000,
            **kw,
        )

    def close(self) -> dict:
        """Stop the model; each vector core's counters from its log."""
        self.stop()
        self.t.close()
        return counters(self.t.model_log)


#: Seconds a silicon wait polls back to back before sleeping between polls:
#: MNIST's kernels run ~0.16 ms each at the low profile, against a 20 ms sleep.
SPIN = 0.05

#: Ways to reach the card: JTAG through Vivado's Tcl server (any host that
#: runs Vivado), XDMA on a Linux host whose PCIe slot holds the card.
TRANSPORTS = ("jtag", "xdma")


def open_transport(kind: str = "jtag"):
    """A transport of `kind` (`TRANSPORTS`). XDMA recovers a hung engine by a
    link reset when ``$XDMA_KO`` names the driver module."""
    if kind == "jtag":
        return JtagTransport()
    if kind == "xdma":
        return XdmaLinux(recover="auto" if os.environ.get("XDMA_KO") else None)
    raise ValueError(f"transport {kind!r}: one of {TRANSPORTS}")


class SiliconCard(NodeCard):
    """The card itself: `transport` (an instance, or a `TRANSPORTS` name;
    JTAG by default) for everything; a wait polls back to back for `spin`
    seconds, then sleeps `poll` seconds between polls; firmware streamed
    through each node's load window.

    Opens only at `profile` ("idle": the board's idle profile) on every node
    used, else raises :class:`ClockMismatch`: a session never starts at rates
    nobody chose. `profile=None` skips the check (a clock ladder sets them)."""

    def __init__(
        self,
        board: str,
        transport=None,
        poll: float = 0.02,
        profile: str | None = "idle",
        idle=None,
        spin: float = SPIN,
        **kw,
    ) -> None:
        if transport is None or isinstance(transport, str):
            transport = open_transport(transport or "jtag")
        t = transport
        b = load_board(board)
        want = b.get("idle_profile", "low") if profile == "idle" else profile
        if want is not None:
            clocks = CardClocks(t, b)
            for n in kw.get("nodes", (0,)):
                got = clocks.mesh(n).read_all()
                rates = b["profiles"][want]
                off = {
                    k: (got[k], v) for k, v in rates.items() if abs(got[k] - v) > 1e-6
                }
                if off:
                    raise ClockMismatch(
                        f"node {n} is not at profile {want!r}: (now, want) {off}; "
                        f"set the clocks first (CardClocks.set_all({want!r}))"
                    )
        idle = idle or (lambda: time.sleep(poll))
        # The sysnode clock the firmware counts its waits in.
        sys_hz = b["profiles"][want]["sys"] * 1e6 if want is not None else None
        super().__init__(
            t, t, board, idle=idle, boot="axi", spin=spin, sys_hz=sys_hz, **kw
        )

    def close(self) -> dict:
        """Stop every node's firmware; the card keeps its clocks."""
        self.stop()
        return {}


def counters(log: pathlib.Path) -> dict:
    """Per vector core, keyed ``DIE.CORE`` (the instance holding the mesh, then
    the core's): the VRES totals and the V2MATH issue breakdown that
    `v2_core.v` prints at the end of simulation."""
    out: dict = {}
    for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"(VRES|V2MATH) (\S+) (.*)", ln)
        if not m:
            continue
        die, _, inner = m[2].partition(".u_mesh0.")
        core = f"{die.rsplit('.', 1)[-1]}.{inner.split('.')[0]}"
        rest = m[3].split()
        fields = {rest[i]: int(rest[i + 1]) for i in range(0, len(rest) - 1, 2)}
        out.setdefault(core, {})["vres" if m[1] == "VRES" else "math"] = fields
    return out


def report(stats: dict) -> list:
    """One line per busy vector core."""
    lines = []
    for core, c in sorted(stats.items()):
        res, math = c.get("vres", {}), c.get("math", {})
        if not res.get("busy"):
            continue
        lines.append(
            f"{core}: "
            + " ".join(f"{k} {v}" for k, v in res.items())
            + " | math "
            + " ".join(f"{k} {v}" for k, v in math.items())
        )
    return lines


def boards() -> list:
    """Every board file's name."""
    return sorted(json.loads(p.read_text())["name"] for p in BOARDS.glob("*.json"))


__all__ = [
    "TRANSPORTS",
    "CardMismatch",
    "ClockMismatch",
    "NodeCard",
    "SiliconCard",
    "VerilatorCard",
    "boards",
    "counters",
    "node_queue",
    "open_transport",
    "report",
]
