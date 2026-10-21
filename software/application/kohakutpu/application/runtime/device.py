"""The device tensors live on and kernels run on: the Verilated card by
default (``KOHAKUTPU_DEVICE=sim``, its model build from
``KOHAKUTPU_SIM_BUILD``), or the card itself (``KOHAKUTPU_DEVICE=card``)
over ``KOHAKUTPU_TRANSPORT`` (jtag, the default, or xdma on a Linux host),
or nothing attached (``KOHAKUTPU_DEVICE=offline``: every run compiled,
checked and encoded, nothing executed). ``KOHAKUTPU_BOARD`` names the board
each opens.

    dev = runtime.device()              # opened once, shared
    a = dev.alloc(nbytes)               # an address in the card's DRAM
    dev.write(a, raw); dev.read(a, n)
    dev.run(compiled, {"x": a, ...})    # scratch allocated and freed here

Every run is checked by the static verifier before the card sees it (the
card's `run` schedules each package through `verify.schedule`).
"""

import os
import pathlib
import tempfile
import time
from dataclasses import dataclass, field

from kohakutpu.application.runtime.arena import OutOfMemory
from kohakutpu.application.runtime.card import TRANSPORTS, SiliconCard, VerilatorCard
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.compiler.layout.buffer import Buffer
from kohakutpu.driver.clock.card import load_board

ROOT = pathlib.Path(__file__).resolve().parents[5]
DEVICE_ENV = "KOHAKUTPU_DEVICE"
BUILD_ENV = "KOHAKUTPU_SIM_BUILD"
#: How many nodes (sysnodes sharing memory through the Xache) the card has.
NODES_ENV = "KOHAKUTPU_SIM_NODES"
#: The board a device opens (boards/<name>.json); a `sim` device without it
#: opens its model's default.
BOARD_ENV = "KOHAKUTPU_BOARD"
BOARD = "multimesh_v9t4"
#: An `offline` device's board: the current 4 + 2 die.
OFFLINE_BOARD = "multimesh_v9t8"
#: `sim` the Verilated card, `card` the card itself, `offline` nothing
#: attached (every run compiled, checked and encoded; nothing executed).
KINDS = ("sim", "card", "offline")
#: How a `card` device reaches the card (`card.TRANSPORTS`).
TRANSPORT_ENV = "KOHAKUTPU_TRANSPORT"
#: The private DRAM arena's most bytes (an eighth of the free DRAM below that).
PRIVATE_MAX = 128 << 20


#: The card models' build root: fast models (no probes) with V2 vector cores
#: and 256 MB of DRAM (`scripts/py/card_model.py card_v9_<n>n_v2 --build-root
#: build/v9g -d VEC_CORE_V2 -G DRAM_WORDS=1048576`).
SIM_ROOT = "v9g"


def default_build(nodes: int) -> pathlib.Path:
    return ROOT / "build" / SIM_ROOT / f"vlt_card_v9_{nodes}n_v2"


DEFAULT_BUILD = default_build(1)
#: Allocations end this far past their bytes: a kernel's last trip may run
#: over its buffer's tail (rows rounded up to a whole band or tile).
TAIL = 16 << 10


@dataclass
class Run:
    """One kernel run: its name, the card's cycles, the wall time."""

    name: str
    cycles: int
    wall: float
    kind: str = "dsl"
    info: dict = field(default_factory=dict)


class Device:
    """A KohakuTPU card of one or more nodes sharing memory: kernel runs,
    counters. Each argument left None comes from its environment variable."""

    def __init__(
        self,
        kind: str | None = None,
        build=None,
        log=None,
        nodes: int | None = None,
        board: str | None = None,
        transport: str | None = None,
    ) -> None:
        kind = kind or os.environ.get(DEVICE_ENV, "sim")
        if kind not in KINDS:
            raise ValueError(f"{DEVICE_ENV}={kind!r}: one of {', '.join(KINDS)}")
        board = board or os.environ.get(BOARD_ENV)
        # The card itself runs on every node its board has.
        whole = load_board(board or BOARD)["meshes"] if kind == "card" else 1
        nodes = int(nodes or os.environ.get(NODES_ENV) or whole)
        self.kind, self.nodes = kind, nodes
        if kind == "offline":
            self.build = None
            self.card = OfflineCard(board or OFFLINE_BOARD, nodes=tuple(range(nodes)))
        elif kind == "card":
            self.build = None
            self.card = SiliconCard(
                board or BOARD,
                transport=transport or os.environ.get(TRANSPORT_ENV, "jtag"),
                nodes=tuple(range(nodes)),
            )
        else:
            build = pathlib.Path(
                build or os.environ.get(BUILD_ENV) or default_build(nodes)
            )
            if not build.exists():
                raise FileNotFoundError(
                    f"no card model at {build}; build one (scripts/py/card_model.py) "
                    f"or set {BUILD_ENV}"
                )
            log = (
                log or pathlib.Path(tempfile.mkdtemp(prefix="ktpu_dev_")) / "model.log"
            )
            self.build = build
            named = {"board": board} if board else {}
            self.card = VerilatorCard(
                build, nodes=tuple(range(nodes)), log=log, **named
            )
        self.machine = self.card.machine
        #: The card's memory, which every allocation here comes from.
        self.memory = self.card.memory
        self.dram = self.memory.dram
        #: Where allocations go while recording (`record`): spans carved out
        #: of the card's memory, so what a recorded log writes is free again
        #: whenever these arenas are as it found them.
        self.private: dict = {
            "dram": self.memory.carve(min(PRIVATE_MAX, self.dram.free // 8))
        }
        for n, st in self.memory.staging.items():
            self.private[n] = self.memory.carve(st.free // 4, node=n)
        self.recording = False
        self.runs: list = []
        self.counters = {"runs": 0, "cycles": 0, "bytes_in": 0, "bytes_out": 0}
        #: folded constants uploaded once, by content (`forms.table`)
        self.tables: dict = {}

    # ------------------------------------------------------------ memory
    def alloc(self, nbytes: int, space: str = "dram", node: int | None = None) -> int:
        if space == "stage":
            n = self.card.nodes[0] if node is None else node
            if self.recording:
                return self.private[n].alloc(nbytes)
            return self._take(lambda: self.memory.stage(nbytes, n))
        if self.recording:
            return self.private["dram"].alloc(int(nbytes) + TAIL)
        return self._take(lambda: self.memory.alloc(int(nbytes) + TAIL))

    def _take(self, alloc) -> int:
        """`alloc()`; when memory runs out, again after the packages spilled
        outside the queues' heaps have given theirs back."""
        try:
            return alloc()
        except OutOfMemory:
            self.card.drop_spilled()
            return alloc()

    def free(self, addr: int) -> None:
        for arena in self.private.values():
            if addr in arena.live:
                arena.release(addr)
                return
        self.memory.free(addr)

    def write(self, addr: int, raw: bytes) -> None:
        raw = bytes(raw)
        # Whole host lines: the memory endpoint fills a short line's rest
        # (a JTAG write), or the host reads it to merge (an XDMA write).
        pad = -len(raw) % self.card.line
        self.card.write(addr, raw + bytes(pad))
        self.counters["bytes_in"] += len(raw)

    def read(self, addr: int, nbytes: int) -> bytes:
        n = -(-int(nbytes) // self.card.line) * self.card.line
        self.counters["bytes_out"] += nbytes
        return bytes(self.card.get(addr, n))[:nbytes]

    # --------------------------------------------------------------- run
    def run(self, compiled, binds: dict, kind: str = "dsl", info=None) -> Run:
        """Run `compiled` (a `kernel.Compiled`) with each parameter at the
        address in `binds`; the L1 body's other parameters (scratch the L2
        pipeline kept) get spans for the run."""
        body = compiled.l1.body(compiled.name, "l1")
        full, scratch = dict(binds), []
        try:
            for p, t in body.params:
                if p in full:
                    continue
                b = Buffer.of(t)
                at = self.alloc(b.nbytes, b.space, b.layout.get("node"))
                scratch.append(at)
                full[p] = at
            t0 = time.monotonic()
            progs = compiled.programs(full)
            single = list(progs) == [None]
            got = self.card.run(progs[None] if single else progs)
            wall = time.monotonic() - t0
        finally:
            for at in scratch:
                self.free(at)
        r = Run(compiled.name, int(got["cycles"]), wall, kind, dict(info or {}))
        self.runs.append(r)
        self.counters["runs"] += 1
        self.counters["cycles"] += r.cycles
        return r

    # ----------------------------------------------------- record / replay
    def record(self) -> None:
        """Log the card's runs and host writes and reads from now on, every
        allocation made from the private arenas."""
        self.recording = True
        self.card.record()

    def recorded(self) -> list:
        """The log since `record` (`card.NodeCard.replay` runs it again)."""
        self.recording = False
        return self.card.recorded()

    def private_state(self) -> tuple:
        """The private arenas' spans: a log is replayed only from the state
        it was recorded in."""
        return tuple(
            (k, tuple(a.free_list), tuple(sorted(a.live.items())))
            for k, a in sorted(self.private.items(), key=str)
        )

    def replay(self, log: list) -> list:
        """`log` run again, nothing recompiled or rebuilt: one `Run` a
        package run, its node cycles (no wall time of its own)."""
        out = []
        for c in self.card.replay(log):
            r = Run("replay", int(c), 0.0, "replay")
            self.runs.append(r)
            self.counters["runs"] += 1
            self.counters["cycles"] += r.cycles
            out.append(r)
        return out

    def close(self) -> dict:
        return self.card.close()

    def __repr__(self) -> str:
        return (
            f"Device({self.kind}: {self.nodes} node(s), {self.machine.count('MG')} MG, "
            f"{self.machine.count('VC')} VC, {self.dram.used:,} B used)"
        )


_device: Device | None = None


def device() -> Device:
    """The process's device, opened the first time it is asked."""
    global _device
    if _device is None:
        _device = Device()
    return _device


def attach(dev: Device) -> Device:
    """Make `dev` the process's device."""
    global _device
    _device = dev
    return dev


__all__ = [
    "BOARD_ENV",
    "BUILD_ENV",
    "DEVICE_ENV",
    "NODES_ENV",
    "TRANSPORTS",
    "TRANSPORT_ENV",
    "Device",
    "Run",
    "attach",
    "default_build",
    "device",
]
