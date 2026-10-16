"""Node programs on a card: boot once, place arrays, run, time, read back.

    card = VerilatorCard(build)                         # the Verilated card
    card = SiliconCard("multimesh_v9t4", nodes=(0,))    # the card, over JTAG
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
import pathlib
import re
import sys
import tempfile
import time

import numpy as np

from kohakuaccel.compiler.machine import STAGE_BYTES
from kohakuaccel.driver.node.boot import BootArgs, NodeBoot
from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.transport.jtag import JtagTransport
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakuaccel.driver.transport.simdram import SimDram
from kohakuaccel.driver.transport.verilator import VerilatorTransport
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
F_TIMING, F_STEPS = 2, 8


class CardMismatch(RuntimeError):
    """The card's enumerated units are not the machine programs are built for."""


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
    ) -> None:
        if check not in verify.GATES:
            raise ValueError(f"check={check!r}: one of {verify.GATES}")
        self.t, self.host = t, host
        self.nodes: tuple[int, ...] = tuple(nodes)
        self.check = check
        #: (node, id(program)) pairs -> (the programs, their findings)
        self.checked: dict = {}
        #: Wall seconds a package may take before its wait raises.
        self.timeout = timeout
        self.board = load_board(board)
        mp = board_map(self.board)
        span = max(self.nodes) + 1
        self.machine = machine or T.machine_of(
            self.board, nodes=span if span > 1 else None
        )
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
        for mesh in card.meshes:
            self.check_units(mesh)
        self.mem = UnitGlobal(host, mp["dram"], mp["mem"], mp["size"], staging=True)
        flags = F_TIMING | (F_STEPS if steps else 0)
        fw = pathlib.Path(fw or FIRMWARE)
        #: node -> its queue, in its own staging store
        self.queues = {}
        for mesh in card.meshes:
            i = mesh.index
            sim = t if boot == "burn" else None
            loader = NodeBoot(t, mp["ctrl"][i], i, sim=sim)
            # A firmware left running (a session that never sent STOP) still
            # writes its queue: stop the core before the header is rewritten.
            loader.win.stop()
            q = NodeQueue(
                self.mem,
                self.machine.stage_addr(0, mesh=i),
                size=Q_SIZE,
                idle=idle,
                spill=lambda n: self.alloc(n),
            )
            q.init()
            args = BootArgs(
                queue=q.base, mesh=i, scan=mesh.caps.grid_hi + 1, flags=flags
            )
            loader.load(fw, args, boot)
            self.queues[i] = q
        for q in self.queues.values():
            q.wait_ready()
        self.q = self.queues[self.nodes[0]]
        self.top = ARENA
        #: node -> (next free, end) of its staging store past its queue
        self.stages = {
            n: (
                self.machine.stage_addr(0, mesh=n) + Q_SIZE,
                self.machine.stage_addr(0, mesh=n) + STAGE_BYTES,
            )
            for n in self.queues
        }
        self.runs = 0
        #: Each node's vector cores' instruction memory and descriptors.
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
    def alloc(self, nbytes: int, align: int = 256) -> int:
        at = -(-self.top // align) * align
        if at + nbytes > self.arena_end:
            raise MemoryError(f"{nbytes} B past the arena ({self.arena_end - at} left)")
        self.top = at + nbytes
        return at

    def mark(self) -> tuple:
        """Where the arena and every staging store are filled to; `release`
        returns them there, freeing everything placed since."""
        return self.top, dict(self.stages)

    def release(self, mark: tuple) -> None:
        self.top, self.stages = mark[0], dict(mark[1])

    def stage(self, nbytes: int, align: int = 256, node: int | None = None) -> int:
        """`nbytes` of a node's MAG staging store (the card's first by
        default), past its queue."""
        n = self.nodes[0] if node is None else node
        top, end = self.stages[n]
        at = -(-top // align) * align
        if at + nbytes > end:
            raise MemoryError(f"{nbytes} B past node {n}'s staging ({end - at} left)")
        self.stages[n] = (at + nbytes, end)
        return at

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
        self.mem.write_block(at, raw)

    def get(self, at: int, nbytes: int) -> bytes:
        return self.mem.read_block(at, nbytes)

    # ------------------------------------------------------------- run
    def run(self, program) -> dict:
        """Run `program` once: ``{"cycles", "steps", "phases", "pkg"}``. Given
        ``{node: Program}`` (None for this card's first node), every node's
        package is submitted before any is waited on; the result is per node
        under ``"nodes"`` and ``"cycles"`` is the longest."""
        single = not isinstance(program, dict)
        progs = {self.nodes[0]: program} if single else program
        progs = {self.nodes[0] if n is None else n: p for n, p in progs.items()}
        self.verify(progs)
        built = B.packages(progs, self.machine, self.fetch, self.residents)
        if single:
            pkg, bindings = built[self.nodes[0]]
            done = self.q.run(pkg.to_bytes(), bindings, timeout=self.timeout)
            self.runs += 1
            return self._result(self.q, done, pkg)
        tags = {}
        for n, (pkg, bindings) in built.items():
            tags[n] = (self.queues[n].submit(pkg.to_bytes(), bindings), pkg)
        out = {}
        for n, (tag, pkg) in tags.items():
            done = self.queues[n].wait(tag, timeout=self.timeout)
            out[n] = self._result(self.queues[n], done, pkg)
        self.runs += 1
        return {"cycles": max(r["cycles"] for r in out.values()), "nodes": out}

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


class SiliconCard(NodeCard):
    """The card itself: `transport` (JTAG by default) for everything, polls
    sleep `poll` seconds, firmware streamed through each node's load window.

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
        **kw,
    ) -> None:
        t = transport or JtagTransport()
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
        super().__init__(t, t, board, idle=idle, boot="axi", **kw)

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
    "CardMismatch",
    "ClockMismatch",
    "NodeCard",
    "SiliconCard",
    "VerilatorCard",
    "boards",
    "counters",
    "report",
]
