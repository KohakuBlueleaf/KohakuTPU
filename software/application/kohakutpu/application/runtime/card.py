"""Node programs on a card: boot once, place arrays, run, time, read back.

    card = VerilatorCard(build, board="multimesh_v9")                         # the Verilated card
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

import dataclasses
import json
import os
import pathlib
import re
import sys
import tempfile
import time

import numpy as np

from kohakuaccel.driver.node.boot import BootArgs, NodeBoot, abi
from kohakuaccel.driver.node.boot.args import REGION_DEBUG, REGION_QUEUE, VERSION, bound
from kohakuaccel.driver.node.queue import (
    BootRefused,
    FirmwareFatal,
    NodeError,
    NodeQueue,
    OverBudget,
)
from kohakuaccel.driver.node.queue import layout as QL
from kohakuaccel.driver.node.queue.client import PreparedPackage
from kohakuaccel.driver.transport.jtag import JtagTransport
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakuaccel.driver.transport.simdram import SimDram
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakuaccel.driver.transport.xdma_linux import XdmaLinux
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime import packages as verify
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.application.runtime.packing import contract_error
from kohakutpu.compiler import _rs
from kohakutpu.driver import units as _units  # noqa: F401  (names MG and VC)
from kohakutpu.driver.clock.card import BOARDS, CardClocks, load_board
from kohakutpu.driver.host import SCRATCH_RESERVE, Card, board_map
from kohakutpu.language import machine as MF

ROOT = pathlib.Path(__file__).resolve().parents[5]
FIRMWARE = ROOT / "build/fw/kohakutpu_node.elf"
#: The debug image (RUL.073): trace and failure records in a debug region.
DEBUG_FIRMWARE = ROOT / "build/fw/kohakutpu_debug.elf"
#: Records the debug region's ring holds (64 bytes each).
DEBUG_RECORDS = 512
#: The debug region's header and last-completion offsets (ka/package/debug.h).
DBG_H_WR, DBG_H_RD, DBG_LASTS = 0x30, 0x40, 0x80
MACHINES = ROOT / "machines"
#: Host arrays start past the node queue's alias at 0.
ARENA = 0x0010_0000
#: The node queue, at the start of each node's staging store.
Q_SIZE = 0x0010_0000
#: Each node queue's submission and completion ring depth: a replayed step
#: (`NodeCard.replay`) submits its packages back to back.
RING = 64
#: Distinct program sets whose static-checker verdicts a card caches (`verify`).
CHECKED_KEPT = 4096
F_TIMING, F_STEPS = 2, 8
#: Boot flags OR'ed into every node's (KA_BOOT_F_*: 1 serial, 4 no fetch,
#: 16 no dispatch engine, 32 no queue entry read ahead).
FLAGS_ENV = "KOHAKUTPU_BOOT_FLAGS"
#: "0": every package carries its vector images whole (no residency).
RESIDENT_ENV = "KOHAKUTPU_RESIDENT"
#: A directory: a package that fails is kept there (bytes and bindings).
FAILED_ENV = "KOHAKUTPU_KEEP_FAILED"


def firmware_wait_seconds(host_timeout: float, requested: float | None) -> float:
    """Default budget: one wait, one failure quiesce, one report/host interval."""
    return host_timeout / 3 if requested is None else requested


def node_queue(mem, base: int, memory: CardMemory, **kw) -> NodeQueue:
    """A node's queue at `base`: packages use the shared allocator and retire
    after their final submitted reference completes."""
    return NodeQueue(
        mem,
        base,
        size=Q_SIZE,
        sq_n=RING,
        cq_n=RING,
        spill=lambda size: memory.alloc(size, owner="package"),
        unspill=memory.free,
        **kw,
    )


def cut(pkg, after: int):
    """`pkg` ending after its step `after`: the host's cut for a dump after
    step k (RUL.076). Raises `ValueError` for a step the package lacks."""
    if not 0 <= after < len(pkg.steps):
        raise ValueError(
            f"DBG.DUMP.001: no step {after}; the package has {len(pkg.steps)}"
        )
    return dataclasses.replace(pkg, steps=pkg.steps[: after + 1])


class CardMismatch(RuntimeError):
    """The card's enumerated units are not the machine programs are built for."""


#: The unit kinds a KohakuTPU mesh holds (matmul clusters, vector cores,
#: Streaming Row Units).
KINDS = ("MG", "VC", "SR")


def enumerated(meshes) -> dict:
    """``{kind: [coord, ..]}`` of the units every one of `meshes` answered
    with; meshes that differ raise :class:`CardMismatch` (the machine is one
    die repeated), and so does a mesh where no unit answered its CU_CAPS read:
    the machine built from it would have no unit at all."""
    found = [{k: sorted(m.coords(k)) for k in KINDS} for m in meshes]
    for m, f in zip(meshes, found):
        if not any(f.values()):
            raise CardMismatch(
                f"mesh {m.index}: no unit answered the host's CU_CAPS reads "
                f"(endpoints {[(e.coord, e.name) for e in m.endpoints]}); "
                f"the agent got no reply from any unit"
            )
    if any(f != found[0] for f in found):
        raise CardMismatch(f"the card's meshes hold different units: {found}")
    return found[0]


def build_facts(machine_json: str) -> dict:
    """`target.machine_of` keywords from a machine file (machines/*.json):
    the vector cores' register count and read copies, the mover's facts."""
    m = json.loads(machine_json)["machine"]
    return {
        "vregs": m["vc"]["regs"],
        "vcopies": m["vc"]["rf_copies"],
        "mover": {"one_word_runs": m["mover"]["one_word_runs"]},
    }


@dataclasses.dataclass
class Pending:
    """A submitted run (`NodeCard.submit`): one package and tag per node,
    the unlowered packages (a failure's debug rerun) and, once collected,
    the result."""

    single: bool
    tags: dict
    plain: dict
    result: dict | None = None


class PreparedEvents(list):
    """Replay event wrappers and package uploads owned by one recording."""

    def __init__(self, source):
        super().__init__()
        self.source = self.snapshot(source)
        self.uploads = []
        self.closed = False

    @staticmethod
    def snapshot(source):
        return tuple(
            (
                (
                    "run",
                    tuple(
                        sorted(
                            (n, bytes(raw), tuple(b)) for n, (raw, b) in ev[1].items()
                        )
                    ),
                )
                if ev[0] == "run"
                else ("write", ev[1], bytes(ev[2]))
            )
            for ev in source
        )

    def matches(self, source):
        return not self.closed and self.source == self.snapshot(source)

    def close(self):
        if not self.closed:
            self.closed = True
            for upload in self.uploads:
                upload.close()
            self.uploads.clear()


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
        arena_end: int | None = None,
        spin: float = 0.0,
        sys_hz: float | None = None,
        wait_seconds: float | None = None,
        debug: bool = False,
        stop_timeout: float = 5.0,
        clock=time.monotonic,
        alarm: float = 0.0,
    ) -> None:
        if check not in verify.GATES:
            raise ValueError(f"check={check!r}: one of {verify.GATES}")
        self.t, self.host = t, host
        #: Seconds a node has to complete STOP (`stop`) before it is restarted.
        self.stop_timeout = stop_timeout
        #: The events since `record`, None while not recording.
        self.log: list | None = None
        self.nodes: tuple[int, ...] = tuple(nodes)
        self.check = check
        #: (node, id(program)) pairs -> (the programs, their findings)
        self.checked: dict = {}
        #: node -> tags submitted (`submit`) whose report text is not yet
        #: read, in order; node -> text read but not yet split; node -> tag
        #: -> its run's text (`_segment`)
        self._order: dict = {}
        self._stdout: dict = {}
        self._segs: dict = {}
        self.timeout = timeout
        #: `clock() -> seconds` of the node's time, which every wait counts.
        self.clock = clock
        self.board = load_board(board)
        mp = board_map(self.board)
        span = max(self.nodes) + 1
        #: The memory port a fetched dispatch reads through, from the board.
        b = self.board
        self.fetch = tuple(b["fetch_port"]) if "fetch_port" in b else None
        self.dram_bytes = mp["size"]
        arena_end = mp["arena_limit"] if arena_end is None else arena_end
        if not ARENA <= arena_end <= mp["arena_limit"]:
            raise ValueError("arena_end must fit below the machine's DRAM scratch")
        self.arena_end = arena_end
        card = Card.from_board(
            board,
            transport=host,
            which=list(self.nodes),
            verify=False,
            reply_at_agent=True,
        )
        #: The machine file: the build's facts, and the names the debug
        #: decoder gives codes.
        self.machine_json = (MACHINES / f"{self.board['name']}.json").read_text(
            encoding="utf-8"
        )
        # Programs are built for the units the card enumerates, which must be
        # its board's machine file's; the board gives addresses and clocks.
        found = enumerated(card.meshes)
        listed = json.loads(self.machine_json)["machine"]["die"]["units"]
        for kind in KINDS:
            want = sorted(tuple(c) for c in listed[kind.lower()])
            if sorted(tuple(c) for c in found[kind]) != want:
                raise CardMismatch(
                    f"the card's {kind} units are {found[kind]}, machine file "
                    f"{self.board['name']}'s {want}"
                )
        self.machine = machine or machine_of(self.board, nodes=span, units=found)
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
                    self.machine.stage_addr(0, mesh=m.index),
                    self.machine.stage_addr(0, mesh=m.index) + self.machine.stage_bytes,
                )
                for m in card.meshes
            },
            paint=self.mem.write_block if self.paint else None,
        )
        # Timing comes from run records; PKGT text is opt-in via FLAGS_ENV.
        flags = (F_STEPS if steps else 0) | int(os.environ.get(FLAGS_ENV, "0"), 0)
        # The firmware's wait bound, from seconds at the profile's sysnode clock
        # (RUL.033), inside the host's own wait (DBG.SESS.003).
        hz = sys_hz or self.board["profiles"][self.board["idle_profile"]]["sys"] * 1e6
        #: The sysnode clock the firmware counts its waits in.
        self.sys_hz = hz
        seconds = firmware_wait_seconds(self.timeout, wait_seconds)
        #: node -> (base, bytes) of its debug region, assigned once
        self.debug_regions: dict = {}
        #: node -> its queue, in its own staging store
        self.queues = {}
        #: node -> (its loader, its boot block), for :meth:`restart`
        self.boots = {}
        for mesh in card.meshes:
            i = mesh.index
            sim = t if boot == "burn" else None
            loader = NodeBoot(t, mp["ctrl"][i], i, sim=sim)
            # A firmware left running (a session that never sent STOP) still
            # writes its queue: stop the core before the header is rewritten.
            loader.win.stop()
            queue_base = self.memory.stage(Q_SIZE, i, owner=f"firmware:queue:{i}")
            q = node_queue(
                self.mem,
                queue_base,
                self.memory,
                idle=idle,
                spin=spin,
                timeout=self.timeout,
                clock=clock,
                alarm=alarm,
            )
            args = BootArgs(
                queue=q.base,
                mesh=i,
                scan=mesh.caps.grid_hi + 1,
                flags=flags,
                timeout=bound(seconds, hz / 1e6),
            )
            self.queues[i] = q
            self.boots[i] = (loader, args)
        self.q = self.queues[self.nodes[0]]
        self.runs = 0
        self._place = boot
        self.load(fw, debug=debug)

    def load(self, fw=None, debug: bool = False) -> None:
        """Every node stopped and booted with image `fw` (by default the
        production image, or the debug image with `debug`): queues
        re-initialised, the vector cores' resident images forgotten. A debug
        image gets a debug region (assigned once per node, kept across loads).
        Raises `abi.AbiMismatch` for an image of another boot block version,
        or a debug load of an image without debug features (DBG.SEL.003)."""
        fw = pathlib.Path(fw or (DEBUG_FIRMWARE if debug else FIRMWARE))
        #: What the image speaks (its .ka_abi words).
        got = abi.check(fw, VERSION, require=True)
        conversion = json.loads(self.machine_json)["machine"]["conversion"]["fp16_mx7"]
        if conversion["version"] >= 2 and not got.get("package_features", 0) & 1:
            raise abi.AbiMismatch(
                f"{fw}: missing package contract/address capability; rebuild firmware"
            )
        if debug and not got["debug"]:
            raise abi.AbiMismatch(f"{fw}: no debug features (DBG.SEL.003)")
        self.abi, self.debug, self.fw = got, debug, fw
        record = self.record_layout(got["queue"])
        for i, (loader, args) in self.boots.items():
            loader.win.stop()
            self.queues[i].speak(got["queue"], record)
            self.queues[i].track_ends = True
            self.queues[i].init()
            q = self.queues[i]
            args.regions = [(q.base, q.size, REGION_QUEUE)]
            if debug:
                if i not in self.debug_regions:
                    units = json.loads(self.machine_json)["machine"]["fw"][
                        "max_pkg_units"
                    ]
                    rec = (DBG_LASTS + units * (1 + got["last"]) * 8 + 63) & ~63
                    size = rec + DEBUG_RECORDS * 64
                    self.debug_regions[i] = (
                        self.memory.alloc(size, owner=f"firmware:debug:{i}"),
                        size,
                    )
                base, size = self.debug_regions[i]
                # No header: the firmware writes its own at ready.
                self.mem.write_block(base, bytes(DBG_LASTS))
                args.regions.append((base, size, REGION_DEBUG))
            loader.load(fw, args, self._place)
        for i in self.queues:
            self._wait_ready(i)
            self._forget(i)
        #: Each node's vector cores' instruction memory and descriptors.
        self.residents = {i: _rs.Cores(self.machine_json) for i in self.queues}
        self.resident = self.residents[self.nodes[0]]

    def _wait_ready(self, i: int, timeout: float = 120.0) -> None:
        loader, _ = self.boots[i]
        try:
            self.queues[i].wait_ready(timeout)
        except FirmwareFatal as e:
            console = getattr(loader, "console", None)
            if not e.console and console is not None:
                tap = console()
                if tap:
                    raise type(e)(e.state, tap) from None
            raise

    def record_layout(self, version: int) -> "QL.Record | None":
        """The schema's run record layout, or None before queue version 4."""
        if version < 4:
            return None
        rec = json.loads(self.machine_json)["machine"]["fw"]["queue"].get("record")
        if rec is None:
            raise abi.AbiMismatch(
                f"{self.board['name']}: the image serves queue v{version}; its machine "
                "file has no fw.queue.record (regenerate machines/)"
            )
        return QL.Record.from_schema(rec)

    @property
    def timeout(self) -> float:
        """Seconds on the queues' clock (the node's time) a package may take
        before its wait raises; every node queue's wait bound."""
        return self._timeout

    @timeout.setter
    def timeout(self, seconds: float) -> None:
        self._timeout = seconds
        for q in getattr(self, "queues", {}).values():
            q.timeout = seconds

    @property
    def alarm(self) -> float:
        """Wall seconds any one wait runs before it raises `OverBudget` (0:
        none); every node queue's."""
        return self.q.alarm

    @alarm.setter
    def alarm(self, seconds: float) -> None:
        for q in self.queues.values():
            q.alarm = seconds

    def restart(self, ready: float = 120.0, nodes=None) -> None:
        """Each node of `nodes` (every node by default) has its firmware
        started again from the image its instruction memory still holds,
        after a reset that kept the arrays (a wizard relock resets every
        sysnode and mesh; DRAM, staging and the placed buffers survive).
        Queues re-initialised; the vector cores' resident images are
        forgotten, so the next run installs them again. Raises
        `TimeoutError` when a node is not ready within `ready` seconds."""
        which = list(self.boots) if nodes is None else list(nodes)
        for i in which:
            loader, args = self.boots[i]
            loader.win.stop()
            self.queues[i].init()
            self._forget(i)
            loader.restart(self.fw, args)
        for i in which:
            self._wait_ready(i, ready)
            self.residents[i] = _rs.Cores(self.machine_json)
        self.resident = self.residents[self.nodes[0]]

    def _forget(self, n: int) -> None:
        """Node `n`'s queue started afresh: no run of it is waited for."""
        for d in (self._order, self._stdout, self._segs):
            d.pop(n, None)

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
        self._forget_residents(self.nodes)
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
        self._forget_residents(self.nodes)
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
                pending.setdefault(n, []).append(self._submit(n, raw, list(bindings)))
            last = nodes
        self._drain(pending, cycles)
        self.runs += sum(1 for ev in log if ev[0] == "run")
        return cycles

    def prepare_recorded(self, log, budget):
        """Decode and pin immutable recorded packages once, within a byte budget."""
        if any(ev[0] not in ("write", "run") for ev in log):
            raise ValueError("only writes and runs can be prepared")
        total = sum(
            (len(raw) + QL.LINE - 1) // QL.LINE * QL.LINE
            for ev in log
            if ev[0] == "run"
            for raw, _ in ev[1].values()
        )
        held = sum(q.prepared_bytes for q in self.queues.values())
        if held + total > budget:
            raise MemoryError("prepared replay packages exceed the device byte budget")
        prepared = PreparedEvents(log)
        try:
            for ev in log:
                if ev[0] == "write":
                    prepared.append(ev)
                    continue
                programs = {}
                for n, (raw, bindings) in ev[1].items():
                    # The recorded bindings never change: admitted once here,
                    # not at each replay's submission.
                    B.admit(raw, self.machine_json, n, tuple(bindings or ()))
                    upload = self.queues[n].prepare(raw)
                    prepared.uploads.append(upload)
                    programs[n] = (
                        upload,
                        B.Package(raw, self.machine_json, n),
                        tuple(bindings),
                    )
                prepared.append(("run", programs))
        except BaseException:
            prepared.close()
            raise
        return prepared

    def submit_prepared(self, programs):
        """Publish prepared entries without parsing, hashing or uploading packages."""
        self._forget_residents(programs)
        tags, plain = {}, {}
        for n, (upload, pkg, bindings) in programs.items():
            tag = self._submit(n, upload, bindings)
            tags[n] = (tag, pkg)
            plain[n] = (pkg, bindings)
        return Pending(len(tags) == 1 and self.nodes[0] in tags, tags, plain)

    def submit_recorded(self, programs: dict) -> Pending:
        """Publish one replay event, retaining its own tags for collection."""
        plain = {
            n: (B.Package(raw, self.machine_json, n), list(b))
            for n, (raw, b) in programs.items()
        }
        self._forget_residents(programs)
        tags = {
            n: (self._submit(n, programs[n][0], b), pkg)
            for n, (pkg, b) in plain.items()
        }
        return Pending(len(tags) == 1 and self.nodes[0] in tags, tags, plain)

    def _drain(self, pending: dict, cycles: list) -> None:
        for n, tags in pending.items():
            for tag in tags:
                try:
                    cycles.append(self.queues[n].wait(tag, timeout=self.timeout).cycles)
                    self._segment(n, tag)
                except NodeError as error:
                    self._forget_residents((n,))
                    raise contract_error(error) from error
                except Exception:
                    self._forget_residents((n,))
                    raise
        pending.clear()

    def _keep_failed(self, pkg, bindings, error: NodeError, node: int) -> None:
        where = os.environ.get(FAILED_ENV)
        if not where:
            return
        d = pathlib.Path(where)
        d.mkdir(parents=True, exist_ok=True)
        stem = d / f"failed_{self.runs:05d}_n{node}_t{error.completion.tag:x}"
        stem.with_suffix(".pkg").write_bytes(pkg.to_bytes())
        record = {
            "bindings": [int(b) for b in bindings or ()],
            "node": node,
            "completion": dataclasses.asdict(error.completion),
            "snapshot": error.snapshot,
            "snapshot_error": error.snapshot_error,
            "console": error.console,
        }
        stem.with_suffix(".json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        print(f"kept the failed package at {stem}.pkg", file=sys.stderr, flush=True)

    # ----------------------------------------------------------- debug
    def debug_report(self, node: int | None = None) -> dict:
        """The debug region's records since the last read, decoded by the Rust
        decoder (`_rs.debug_decode`, RUL.080), and marked read on the node.
        Raises when this card is not a debug session."""
        node = self.nodes[0] if node is None else node
        if not self.debug or node not in self.debug_regions:
            raise RuntimeError(
                "DBG.SEL.003: not a debug session: open the card with debug=True"
            )
        base, size = self.debug_regions[node]
        head = self.mem.read_block(base, DBG_LASTS)
        h = [int.from_bytes(head[i : i + 8], "little") for i in range(0, DBG_LASTS, 8)]
        last, units, rec, cap, wr, rd = h[2], h[3], h[4], h[5], h[6], h[8]
        buf = bytearray(size)
        buf[:DBG_LASTS] = head
        lasts = units * (1 + last) * 8
        buf[DBG_LASTS : DBG_LASTS + lasts] = self.mem.read_block(
            base + DBG_LASTS, lasts
        )
        at = rd
        while cap and at < wr:
            off = at % cap
            n = min(wr - at, cap - off)
            buf[rec + off : rec + off + n] = self.mem.read_block(base + rec + off, n)
            at += n
        got = _rs.debug_decode(bytes(buf), self.machine_json)
        self.mem.write_block(base + DBG_H_RD, QL.line(got["consumed"]))
        return got

    @staticmethod
    def drained(report: dict | None, console: str = "") -> bool:
        """Whether a failed run's quiesce drained (DBG.DUMP.002): the debug
        image's FAIL records in `report`, else the production image's last
        ``PKGF stray <n> outstanding <n>`` line in `console`."""
        if report is not None:
            return all(
                p["fail"] is None or p["fail"]["drained"] for p in report["packages"]
            )
        left = re.findall(r"PKGF stray \d+ outstanding (\d+)", console)
        return bool(left) and int(left[-1]) == 0

    def dump(self, buffers: dict, stable: bool = True) -> dict:
        """`buffers` (``{name: (address, bytes)}``, the caller's: a bound
        package carries no buffer entries) read by the host after the
        completion (RUL.076): ``{name: {"bytes", "stable"}}`` (DBG.DUMP.001).
        `stable` False marks bytes a failed run left after a quiesce that did
        not drain (DBG.DUMP.002)."""
        return {
            name: {"bytes": self.get(at, nbytes), "stable": stable}
            for name, (at, nbytes) in buffers.items()
        }

    def _debug_rerun(
        self, node: int, raw: bytes, bindings, report: dict, timeout_cycles: int = 0
    ) -> dict:
        """RUL.075: a failed package once more, off the dispatch engine and
        traced, for exact step attribution, under the failed run's bound
        (`timeout_cycles`, 0 the boot block's); the firmware restarted first
        when the failure did not drain (RUL.082)."""
        if not self.drained(report):
            self.restart()
        q = self.queues[node]
        try:
            q.run(
                raw,
                bindings,
                timeout=self.timeout,
                timeout_cycles=timeout_cycles,
                flags=QL.RUN_F_TRACE | QL.RUN_F_NOENGINE,
            )
        except NodeError:
            pass
        again = self.debug_report(node)
        if any(p["fail"] and not p["fail"]["drained"] for p in again["packages"]):
            self.restart()
        return again

    # ------------------------------------------------------------- run
    def run(
        self,
        program,
        faults: bool = False,
        trace: bool = False,
        flags: int = 0,
        dump=None,
        after: int | None = None,
    ) -> dict:
        """Run `program` once: ``{"cycles", "steps", "phases", "pkg"}``. Given
        ``{node: Program}`` (None for this card's first node), every node's
        package is submitted before any is waited on; the result is per node
        under ``"nodes"`` and ``"cycles"`` is the longest. With `faults`, a
        node that times out or faults reports ``{"fault": text}`` and the
        other nodes are still waited for. `trace` (the debug image only)
        records each step; `flags` are further run flags (`layout.RUN_F_*`).
        `dump` (``{name: (address, bytes)}``) adds :meth:`dump` of it under
        ``"dump"`` (on a failure, the `NodeError`'s ``dump``); `after` cuts
        each package after that step (RUL.076). `collect(submit(...))`."""
        return self.collect(
            self.submit(program, trace=trace, flags=flags, after=after),
            faults=faults,
            dump=dump,
        )

    def submit(
        self, program, trace: bool = False, flags: int = 0, after: int | None = None
    ) -> "Pending":
        """`program` checked, built and queued on its nodes without waiting:
        the node runs its queue in order, so the next package is queued while
        this one runs (L0.NQ.047). :meth:`collect` waits for it."""
        flags |= QL.RUN_F_TRACE if trace else 0
        if flags & (QL.RUN_F_TRACE | QL.RUN_F_NOENGINE) and not self.debug:
            raise RuntimeError(
                f"DBG.SEL.003: {self.fw.name} has no trace; a traced run needs the "
                f"debug image (debug=True)"
            )
        single = not isinstance(program, dict)
        progs = B.placed(program, self.nodes)
        self.verify(progs)
        if os.environ.get(RESIDENT_ENV) == "0":
            for cores in self.residents.values():
                cores.forget()
        # Unlowered: what the firmware path runs (a NOENGINE run, the debug
        # rerun of a failure); lowered: what the dispatch engine runs.
        # A Rust package's cut is the Rust stack's (`_rs.bind(after=)`).
        off = bool(flags & QL.RUN_F_NOENGINE)
        stateless = (
            os.environ.get("KOHAKUTPU_HOST_SUBMIT_CACHE", "1") == "1"
            and os.environ.get(RESIDENT_ENV, "0") == "0"
        )
        if stateless:
            self._forget_residents(progs)
        predicted = (
            None if stateless else {n: self.residents[n].snapshot() for n in progs}
        )
        both = B.built(
            progs, self.machine, self.fetch, None if off else predicted, after
        )
        plain = {n: (pkg, b) for n, (pkg, _, b) in both.items()}
        lowered = {n: (pkg, b) for n, (_, pkg, b) in both.items()}
        built = plain if off else lowered
        tags = {}
        for n, (pkg, bindings) in built.items():
            try:
                # `B.built` admits lowered bytes; NOENGINE admits its own stream.
                tag = self._submit(
                    n, pkg.to_bytes(), bindings, flags=flags, admitted=not off
                )
            except Exception:
                self._forget_residents((n,))
                raise
            tags[n] = (tag, pkg)
            if off or after is not None:
                self._forget_residents((n,))
            elif predicted is not None:
                self.residents[n].restore(predicted[n])
        if self.log is not None:
            self.log.append(
                (
                    "run",
                    {n: (pkg.to_bytes(), tuple(b)) for n, (pkg, b) in built.items()},
                )
            )
        return Pending(single, tags, plain)

    @property
    def publication_epoch(self) -> int:
        """Monotonic count of attempted queue publications, including failed writes."""
        return getattr(self, "_publication_epoch", 0)

    def _publishing(self) -> None:
        self._publication_epoch = self.publication_epoch + 1

    def _submit(self, n: int, raw, bindings, admitted: bool = False, **kw) -> int:
        """A RUN of `raw` (package bytes, or an upload `prepare_recorded`
        admitted) queued on node `n`; its tag, in the node's order of
        report text (`_segment`). `admitted`: the caller's build already
        admitted `raw`."""
        if not admitted and not isinstance(raw, PreparedPackage):
            B.admit(raw, self.machine_json, n, tuple(bindings or ()))
        q, order = self.queues[n], self._order.setdefault(n, [])
        if not order:
            # Text of runs nothing here waits for (a replay, a direct
            # `q.run`): not this run's.
            q.read_stdout()
            self._stdout[n] = ""
        tag = q.submit(raw, bindings, publishing=self._publishing, **kw)
        order.append(tag)
        return tag

    def _segment(self, n: int, tag: int, console: str = "") -> str:
        """Split each run's text at its record's stdout count (L0.NQ.052)."""
        q = self.queues[n]
        if getattr(q, "record", None) is None:
            return self._segment_pkgt(n, tag, console)
        order, done = self._order.setdefault(n, []), self._segs.setdefault(n, {})
        buf = self._stdout.pop(n, "") + console
        while tag in order:
            end = q.ends.pop(order[0], None)
            if end is None:
                break
            start = q.console_at - len(buf)
            if end > q.console_at:
                buf += q.read_stdout_to(end)
            take = max(0, end - start)
            done[order.pop(0)], buf = buf[:take], buf[take:]
        self._stdout[n] = buf
        return done.pop(tag, "")

    def _segment_pkgt(self, n: int, tag: int, console: str = "") -> str:
        """The report text node `n` printed for run `tag`. A RUN ends its text
        with one PKGT line (L0.BOOT.010), so the node's text splits into its
        runs in submission order; text read before (`console`, a failure's)
        is taken first."""
        buf = self._stdout.get(n, "") + console
        order, done = self._order.setdefault(n, []), self._segs.setdefault(n, {})
        while True:
            more = self.queues[n].read_stdout()
            buf += more
            while order and order[0] not in done:
                at = buf.find("PKGT ")
                end = buf.find("\n", at)
                if at < 0 or end < 0:
                    break
                done[order.pop(0)] = buf[: end + 1]
                buf = buf[end + 1 :]
            if tag not in order or not more:
                break
        self._stdout[n] = buf
        return done.pop(tag, "")

    def _forget_residents(self, nodes):
        """Invalidate predicted IMEM after any uncertain execution."""
        for n in nodes:
            cores = getattr(self, "residents", {}).get(n)
            if cores is not None:
                cores.forget()

    def collect(self, p: "Pending", faults: bool = False, dump=None) -> dict:
        """The result of a :meth:`submit`ted run (as :meth:`run` returns it),
        waiting for it; `faults` and `dump` as :meth:`run`'s. Collected
        twice, the first result."""
        if p.result is None:
            try:
                p.result = self._collect(p, faults, dump)
            except NodeError as error:
                self._forget_residents(p.tags)
                raise contract_error(error) from error
            except Exception:
                self._forget_residents(p.tags)
                raise
        return p.result

    def _collect(self, p: "Pending", faults: bool, dump) -> dict:
        single, tags, plain = p.single, p.tags, p.plain
        # Peers may communicate: publish all of them before waiting on any one.
        if not single:
            for node in tags:
                queue = self.queues[node]
                if hasattr(queue, "flush"):
                    queue.flush()
        if single:
            node = self.nodes[0]
            tag, pkg = tags[node]
            bindings = plain[node][1]
            try:
                done = self.q.wait(tag, timeout=self.timeout)
            except TimeoutError:
                self._order[node].remove(tag)
                raise
            except NodeError as e:
                try:
                    self._segment(node, tag, e.console)
                    self._keep_failed(pkg, bindings, e, node)
                    e.debug = self.debug_report(node) if self.debug else None
                    if dump is not None:
                        # Before the rerun, which writes the same buffers.
                        e.dump = self.dump(dump, self.drained(e.debug, e.console))
                    if e.debug is not None:
                        e.rerun = self._debug_rerun(
                            node, plain[node][0].to_bytes(), bindings, e.debug
                        )
                except Exception as diagnostic:  # noqa: BLE001
                    e.add_note(f"failure diagnostics: {diagnostic!r}")
                raise
            self.runs += 1
            res = self._result(self._segment(node, tag), done, pkg)
            if self.debug:
                res["debug"] = self.debug_report(node)
            if dump is not None:
                res["dump"] = self.dump(dump)
            return res
        out = {}
        #: Every failure so far drained (DBG.DUMP.002).
        stable = True
        for k, (n, (tag, pkg)) in enumerate(tags.items()):
            try:
                done = self.queues[n].wait(tag, timeout=self.timeout)
            except TimeoutError as e:
                self._forget_residents((n,))
                # The node may still be running: nothing read is stable.
                stable = False
                self._order[n].remove(tag)
                if not faults:
                    raise
                out[n] = {"fault": str(e), "debug": None}
                continue
            except NodeError as e:
                e = contract_error(e)
                self._forget_residents((n,))
                try:
                    self._segment(n, tag, e.console)
                    self._keep_failed(pkg, plain[n][1], e, n)
                    e.debug = self.debug_report(n) if self.debug else None
                    stable = stable and self.drained(e.debug, e.console)
                    if not faults and dump is not None:
                        e.dump = self.dump(dump, stable and k == len(tags) - 1)
                except Exception as diagnostic:  # noqa: BLE001
                    stable = False
                    e.add_note(f"failure diagnostics: {diagnostic!r}")
                if not faults:
                    raise
                out[n] = {
                    "fault": str(e),
                    "debug": e.debug,
                    "snapshot": e.snapshot,
                    "snapshot_error": e.snapshot_error,
                }
                continue
            try:
                out[n] = self._result(self._segment(n, tag), done, pkg)
                if self.debug:
                    out[n]["debug"] = self.debug_report(n)
            except ValueError as e:
                self._forget_residents((n,))
                # The node's own report is unreadable: its processor misbehaved.
                if not faults:
                    raise
                out[n] = {"fault": f"garbled node report: {e}"}
        self.runs += 1
        cycles = [r["cycles"] for r in out.values() if "cycles" in r]
        res = {"cycles": max(cycles, default=0), "nodes": out}
        if dump is not None:
            res["dump"] = self.dump(dump, stable)
        return res

    def verify(self, progs: dict) -> list:
        """The checker's findings for `progs` (``{node: Program}``), once per
        set of programs: raises `verify.Refused` under ``check="refuse"``."""
        key = tuple(
            sorted(
                (
                    n,
                    (
                        (id(p.package), id(p.findings), p.refused)
                        if isinstance(p, B.Prebuilt)
                        else id(p)
                    ),
                )
                for n, p in progs.items()
            )
        )
        if key not in self.checked:
            found = B.findings(progs, self.check)
            # The programs (or their package bytes and findings) are held so their ids are not reused.
            held = tuple(
                (p.package, p.findings) if isinstance(p, B.Prebuilt) else p
                for p in progs.values()
            )
            self.checked[key] = (held, found)
            while len(self.checked) > CHECKED_KEPT:
                self.checked.pop(next(iter(self.checked)))
            for f in found:
                print(f"verify: {f}", file=sys.stderr, flush=True)
        return self.checked[key][1]

    def _result(self, text: str, done, pkg) -> dict:
        steps, phases = [], {}
        record = getattr(done, "record", None)
        if record is not None:
            phases = {k: v for k, v in record.items() if k not in ("tag", "so_wr")}
        for ln in text.splitlines():
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
        """Ask every node's firmware to leave its loop, each within
        `stop_timeout` seconds. A node that does not complete STOP (a run
        left it busy) is restarted (`restart`, ready within the same bound)
        and asked again; one that still does not is halted at its core
        (`win.stop`). Never waits past those bounds."""
        for i, q in self.queues.items():
            try:
                q.stop(timeout=self.stop_timeout)
                continue
            except (TimeoutError, FirmwareFatal, OverBudget) as e:
                why = f"{type(e).__name__}: {str(e)[:200]}"
            print(
                f"card.stop: node {i} did not complete STOP in {self.stop_timeout}s "
                f"(pending tags {sorted(q._outstanding)}; {why}); restarting it",
                file=sys.stderr,
            )
            try:
                self.restart(ready=self.stop_timeout, nodes=(i,))
                q.stop(timeout=self.stop_timeout)
            except (TimeoutError, RuntimeError) as e:
                print(
                    f"card.stop: node {i} still not stopped after a restart "
                    f"({type(e).__name__}: {str(e)[:200]}); halted at its core",
                    file=sys.stderr,
                )
                self.boots[i][0].win.stop()


#: Sysnode cycles one idle of a Verilated card's wait advances the model.
IDLE_CYCLES = 600
#: Wall seconds a Verilated card's wait runs before `OverBudget`.
WALL_ALARM = 300.0


class VerilatorCard(NodeCard):
    """The Verilated card: host DRAM traffic through the model's arrays
    (`SimDram`), polls advance the model, firmware burnt into its memories."""

    def __init__(
        self, build, board: str | None = None, log=None, args=(), **kw
    ) -> None:
        board = board or os.environ.get("KOHAKUTPU_BOARD")
        if not board:
            raise ValueError("RUL.011: name a board with board= or KOHAKUTPU_BOARD")
        MF.load(board)
        b = load_board(board)
        self.build = pathlib.Path(build)
        # A log of its own: cards run in parallel on one build.
        log = log or pathlib.Path(tempfile.mkdtemp(prefix="ktpu_card_")) / "model.log"
        # `args`: harness flags (sim/verilator/docs/card-backend.md).
        t = VerilatorTransport(self.build, log=log, args=tuple(args))
        mp = board_map(b)
        host = SimDram(t, b, mp["mem"], mp["size"])
        # A mismatched model build must not silently wrap allocated DRAM.
        actual = sum(words * width for _, _, words, width in host.dram)
        if ("model" in b and actual != mp["size"]) or not (
            SCRATCH_RESERVE < actual <= mp["size"]
        ):
            t.close()
            raise ValueError(f"model DRAM {actual} != machine DRAM {mp['size']}")
        if actual < mp["size"]:
            kw.setdefault("arena_end", actual - SCRATCH_RESERVE)
        # The firmware's wait bound: 20 ms of the card's sysnode clock
        # (2,000,000 cycles at 100 MHz). The host's waits count the same
        # modelled time (`clock`), so the firmware's TIMEOUT ends a stuck wait
        # before the host's bound at any model speed; `alarm` is wall seconds.
        kw.setdefault("wait_seconds", 0.02)
        kw.setdefault("stop_timeout", 2 * kw["wait_seconds"])
        kw.setdefault("alarm", WALL_ALARM)
        #: Sysnode cycles the host's idles advanced the model: the queues'
        #: clock (host traffic advances it further, uncounted).
        self.idled = 0
        super().__init__(
            t,
            host,
            board,
            idle=self._idle,
            boot="burn",
            clock=self.modelled,
            **kw,
        )

    def _idle(self) -> None:
        self.t.run(IDLE_CYCLES)
        self.idled += IDLE_CYCLES

    def modelled(self) -> float:
        """Seconds of the card's sysnode clock the host's idles modelled."""
        return self.idled / self.sys_hz

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
    "BootRefused",
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
