"""What the card classes share: paths, sizes, modes, pending and prepared events."""

import dataclasses
import enum
import pathlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as QL
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakutpu.application.runtime.eager_batch import Batch
from kohakutpu.application.runtime.machine import MachineSpec
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.application.runtime.paths import ROOT
from kohakutpu.language.dsl.kernel import _MUTANT

FIRMWARE = ROOT / "build/fw/kohakutpu_node.elf"

#: The debug image (RUL.073): trace and failure records in a debug region.
DEBUG_FIRMWARE = ROOT / "build/fw/kohakutpu_debug.elf"

#: The measure image (KA_MEASURE, no package copy): a record a run in `measure.Ring`.
MEASURE_FIRMWARE = ROOT / "build/fw/kohakutpu_measure.elf"

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

#: Each sysnode's boot flags, as `parse_boot_flags` reads them.
FLAGS_ENV = "KOHAKUTPU_BOOT_FLAGS"

#: KA_BOOT_F_* by name (ka/boot/args.h).
BOOT_FLAGS = {
    "SERIAL": 1,
    "TIMING": 2,
    "NOFETCH": 4,
    "STEPS": 8,
    "NOENGINE": 16,
    "SERIALQ": 32,
}

#: "0": every package carries its vector images whole (no residency).
RESIDENT_ENV = "KOHAKUTPU_RESIDENT"

#: A directory: a package that fails is kept there (bytes and bindings).
FAILED_ENV = "KOHAKUTPU_KEEP_FAILED"


class Unset(enum.Enum):
    """A `resident_mode` nobody gave: the environment's."""

    UNSET = enum.auto()


_UNSET: Final = Unset.UNSET


class ResidentMode(str, enum.Enum):
    UNSET = "unset"
    OFF = "0"
    ON = "1"

    @property
    def forget_all(self) -> bool:
        return self is ResidentMode.OFF

    @property
    def stateless(self) -> bool:
        return self in (ResidentMode.UNSET, ResidentMode.OFF)

    @property
    def resident(self) -> bool:
        return self is ResidentMode.ON


def parse_resident_mode(value: str | ResidentMode | None) -> ResidentMode:
    if isinstance(value, ResidentMode):
        return value
    mutant = _MUTANT.get()
    if value is None:
        if mutant == "app.resident/unset_is_off":
            return ResidentMode.OFF
        return ResidentMode.UNSET
    if value == "0" or (value != "1" and mutant == "app.resident/only_one_resides"):
        return ResidentMode.OFF
    return ResidentMode.ON


def parse_boot_flags(text: str, nodes) -> dict[int, int]:
    """`text` as each of `nodes`' boot flags: one value for all, or `N:V,N:V` (a
    sysnode not named gets 0); a value is a number or `BOOT_FLAGS` names joined by +."""

    def value(v: str) -> int:
        if v[:1].isalpha():
            flags = 0
            for k in v.split("+"):
                flags |= BOOT_FLAGS[k]
            return flags
        return int(v, 0)

    if ":" not in text:
        return dict.fromkeys(nodes, value(text))
    given = dict(part.split(":") for part in text.split(","))
    return {n: value(given.get(str(n), "0")) for n in nodes}


def firmware_wait_seconds(host_timeout: float, requested: float | None) -> float:
    """Default budget: one wait, one failure quiesce, one report/host interval."""
    return host_timeout / 3 if requested is None else requested


def node_queue(mem, base: int, memory: CardMemory, node: int = 0, **kw) -> NodeQueue:
    """A node's queue at `base`: packages use the shared allocator, placed for
    `node`, and retire after their final submitted reference completes."""
    return NodeQueue(
        mem,
        base,
        size=Q_SIZE,
        sq_n=RING,
        cq_n=RING,
        spill=lambda size: memory.alloc(size, owner="package", near=memory.near(node)),
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


class Parts:
    """What `NodeCard`'s parts read of one another, declared only: `NodeCard` or a
    sibling part defines each."""

    nodes: tuple[int, ...]
    queues: dict[int, NodeQueue]
    q: NodeQueue
    mem: UnitGlobal
    memory: CardMemory
    machine: MachineSpec
    machine_json: str
    queue_layout: QL.Layout
    fetch: tuple | None
    fw: pathlib.Path
    debug: bool
    debug_regions: dict
    check: str
    checked: dict
    log: list | None
    residents: dict
    runs: int
    _eager_batch: Batch | None
    _publication_epoch: int
    _order: dict
    _stdout: dict
    _segs: dict
    collect: Callable[..., dict]
    get: Callable[[int, int], bytes]
    restart: Callable[..., None]
    verify: Callable[[dict], list]
    _drain: Callable[[dict, list], None]
    _forget_residents: Callable[..., None]
    _segment: Callable[..., str]
    _submit: Callable[..., int]

    if TYPE_CHECKING:

        @property
        def admission_cached(self) -> bool: ...

        @property
        def resident_mode(self) -> ResidentMode: ...

        @property
        def submit_cache(self) -> bool: ...

        @property
        def timeout(self) -> float: ...


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


@dataclasses.dataclass
class Pending:
    """A submitted run (`NodeCard.submit`): one package and tag per node,
    the unlowered packages (a failure's debug rerun) and, once collected,
    the result."""

    single: bool
    tags: dict
    plain: dict
    result: dict | None = None
    execution: bool = False


class PreparedEvents(list):
    """Replay event wrappers and package uploads owned by one recording."""

    budget: int
    stream_workspace: list
    selection: dict

    def __init__(self, source):
        super().__init__()
        self.source = self.snapshot(source)
        self.uploads = []
        self.execution_lists = []
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
            for item in self.execution_lists:
                item.close()
            for upload in self.uploads:
                upload.close()
            self.uploads.clear()


class ClockMismatch(RuntimeError):
    """A node's wizard is not at the profile the card was opened for."""


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
