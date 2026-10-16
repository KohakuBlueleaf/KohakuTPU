"""The device tensors live on and kernels run on: the Verilated card by
default (``KOHAKUTPU_DEVICE=sim``, its model build from
``KOHAKUTPU_SIM_BUILD``), or the card itself over JTAG
(``KOHAKUTPU_DEVICE=card``, its board from ``KOHAKUTPU_BOARD``).

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

from kohakutpu.application.runtime.arena import Arena
from kohakutpu.application.runtime.card import SiliconCard, VerilatorCard
from kohakutpu.compiler.layout.buffer import Buffer

ROOT = pathlib.Path(__file__).resolve().parents[5]
DEVICE_ENV = "KOHAKUTPU_DEVICE"
BUILD_ENV = "KOHAKUTPU_SIM_BUILD"
#: How many nodes (sysnodes sharing memory through the Xache) the card has.
NODES_ENV = "KOHAKUTPU_SIM_NODES"
#: The board a `card` device opens (boards/<name>.json).
BOARD_ENV = "KOHAKUTPU_BOARD"
BOARD = "multimesh_v9t4"


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
    counters."""

    def __init__(
        self, kind: str | None = None, build=None, log=None, nodes: int | None = None
    ) -> None:
        kind = kind or os.environ.get(DEVICE_ENV, "sim")
        if kind not in ("sim", "card"):
            raise ValueError(f"{DEVICE_ENV}={kind!r}: one of sim, card")
        nodes = int(nodes or os.environ.get(NODES_ENV) or 1)
        self.kind, self.nodes = kind, nodes
        if kind == "card":
            self.build = None
            board = os.environ.get(BOARD_ENV, BOARD)
            self.card = SiliconCard(board, nodes=tuple(range(nodes)))
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
            self.card = VerilatorCard(build, nodes=tuple(range(nodes)), log=log)
        self.machine = self.card.machine
        self.dram = Arena(self.card.top, self.card.arena_end)
        self.staging = {
            n: Arena(lo, hi, align=256) for n, (lo, hi) in self.card.stages.items()
        }
        self.runs: list = []
        self.counters = {"runs": 0, "cycles": 0, "bytes_in": 0, "bytes_out": 0}

    # ------------------------------------------------------------ memory
    def alloc(self, nbytes: int, space: str = "dram", node: int | None = None) -> int:
        if space == "stage":
            n = self.card.nodes[0] if node is None else node
            return self.staging[n].alloc(nbytes)
        return self.dram.alloc(int(nbytes) + TAIL)

    def free(self, addr: int) -> None:
        for arena in (self.dram, *self.staging.values()):
            if addr in arena.live:
                arena.release(addr)
                return
        raise KeyError(f"{addr:#x} was never allocated")

    def write(self, addr: int, raw: bytes) -> None:
        raw = bytes(raw)
        # Whole 32-byte lines: the memory endpoint fills a short line's rest.
        pad = -len(raw) % 32
        self.card.write(addr, raw + bytes(pad))
        self.counters["bytes_in"] += len(raw)

    def read(self, addr: int, nbytes: int) -> bytes:
        n = -(-int(nbytes) // 32) * 32
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
    "Device",
    "Run",
    "attach",
    "default_build",
    "device",
]
