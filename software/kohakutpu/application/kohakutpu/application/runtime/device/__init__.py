"""The device tensors live on and kernels run on: the Verilated card by
default (``KOHAKUTPU_DEVICE=sim``, its model build from
``KOHAKUTPU_SIM_BUILD``), or the card itself (``KOHAKUTPU_DEVICE=card``)
over ``KOHAKUTPU_TRANSPORT`` (jtag, the default, or xdma on a Linux host),
or nothing attached (``KOHAKUTPU_DEVICE=offline``: every run compiled,
checked and encoded, nothing executed), or ``KOHAKUTPU_DEVICE=double``: the
same, each run's L2 on the value simulator. ``KOHAKUTPU_BOARD`` names the
board each opens.

    dev = runtime.device()              # opened once, shared
    a = dev.alloc(nbytes)               # an address in the card's DRAM
    dev.write(a, raw); dev.read(a, n)
    dev.run(compiled, {"x": a, ...})    # scratch in its space's window

Every run is checked by the static verifier before the card sees it (the
card's `run` schedules each package through `verify.schedule`).
"""

import pathlib
import tempfile
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from typing import Any

from kohakuaccel.driver.runtime.mmu import SoftwareMMU
from kohakutpu.application.runtime import replay_budget
from kohakutpu.application.runtime.card import (
    RESIDENT_ENV,
    TRANSPORTS,
    ResidentMode,
    SiliconCard,
    VerilatorCard,
    parse_resident_mode,
)
from kohakutpu.application.runtime.device.allocation import Allocation
from kohakutpu.application.runtime.device.common import (
    BOARD_ENV,
    BUILD_ENV,
    DEVICE_ENV,
    KINDS,
    MAX_INFLIGHT,
    NODES_ENV,
    ON_DEVICE_INIT,
    PACKAGE_HOME_ENV,
    PREPARED_REPLAY_ENV,
    PROFILE_ENV,
    QUEUE_BYTES_ENV,
    QUEUE_DEPTH_ENV,
    QUEUE_ENV,
    REPLAY_PACKAGE_BYTES_ENV,
    TRANSPORT_ENV,
    Recorded,
    Run,
    Submitted,
    default_build,
)
from kohakutpu.application.runtime.device.kernels import Kernels
from kohakutpu.application.runtime.device.replay import Replay
from kohakutpu.application.runtime.device.submission import Submission
from kohakutpu.application.runtime.double import DoubleCard
from kohakutpu.application.runtime.memory import package_homes
from kohakutpu.application.runtime.native import direct as native_direct
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.application.runtime.os.card import OSCard
from kohakutpu.application.runtime.paths import ROOT
from kohakutpu.driver.clock.card import load_board
from kohakutpu.language import machine as MF
from kohakutpu.language.config import ENV
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import arena_name


class Device(Kernels, Allocation, Submission, Replay):
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
        queued: bool | None = None,
        queue_bytes: int | None = None,
        library_only: bool = False,
        library=None,
        classes: bool | None = None,
        queue_depth: int | None = None,
        os_session=None,
        os_units=None,
    ) -> None:
        self.recipe_epoch = rs._rs.RecipeEpoch()
        self._library = self._machine = None
        self.kernels: OrderedDict = OrderedDict()
        self.specialisations: OrderedDict = OrderedDict()
        self.eager_schedules = None
        self.eager_calls = None
        self.eager_sequences = None
        self.eager_recipes = None
        self._recipe_capture = None
        self._recipe_recording = None
        self.eager_exec = ENV.get("KTPU_EAGER_EXEC", "1") == "1"
        self.auto_sequence = ENV.get("KTPU_AUTO_SEQUENCE", "1") == "1"
        self.eager_recipe_enabled = ENV.get("KTPU_EAGER_RECIPE", "1") == "1"
        self.native_recipe = ENV.get("KTPU_NATIVE_RECIPE", "1") == "1"
        self.wire_recipe = ENV.get("KTPU_WIRE_RECIPE", "1") == "1"
        self.eager_flush = ENV.get("KOHAKUTPU_EAGER_FLUSH", "1") == "1"
        self.specialise_cache = ENV.get("KTPU_SPECIALISE_CACHE", "1") == "1"
        self._resident_mode = parse_resident_mode(ENV.get(RESIDENT_ENV))
        class_mode = ENV.get("KOHAKUTPU_LIBRARY_CLASSES", "1")
        if classes is None and class_mode not in ("0", "1"):
            raise ValueError("KOHAKUTPU_LIBRARY_CLASSES must be 0 or 1")
        self.classes = class_mode == "1" if classes is None else classes
        mode = ENV.get(QUEUE_ENV, "1")
        if queued is None and mode not in ("0", "1"):
            raise ValueError(f"{QUEUE_ENV} must be 0 or 1")
        self.queued = mode == "1" if queued is None else queued
        budget = queue_bytes if queue_bytes is not None else ENV.get(QUEUE_BYTES_ENV)
        budget = None if budget is None else int(budget)
        if budget is not None and budget < 1:
            raise ValueError("queue_bytes must be positive")
        depth = queue_depth if queue_depth is not None else ENV.get(QUEUE_DEPTH_ENV)
        depth = MAX_INFLIGHT if depth is None else int(depth)
        if depth is not None and depth < 1:
            raise ValueError("queue_depth must be positive or None")
        prepared = ENV.get(PREPARED_REPLAY_ENV, "1")
        if prepared not in ("0", "1"):
            raise ValueError(f"{PREPARED_REPLAY_ENV} must be 0 or 1")
        self.prepared_replay = prepared == "1"
        replay_bytes = ENV.get(REPLAY_PACKAGE_BYTES_ENV)
        self.replay_package_bytes = None if replay_bytes is None else int(replay_bytes)
        if self.replay_package_bytes is not None and self.replay_package_bytes < 0:
            raise ValueError("replay package byte budget must be nonnegative")
        self.replay_package_fraction = float(
            ENV.get(replay_budget.FRACTION_ENV, str(replay_budget.DEFAULT_FRACTION))
        )
        if not 0.0 <= self.replay_package_fraction <= 1.0:
            raise ValueError("replay package fraction must be in [0, 1]")
        self.replay_overcommit = ENV.get(replay_budget.OVERCOMMIT_ENV, "0") == "1"
        kind = kind or ENV.get(DEVICE_ENV, "sim")
        if kind not in KINDS:
            raise ValueError(f"{DEVICE_ENV}={kind!r}: one of {', '.join(KINDS)}")
        board = board or ENV.get(BOARD_ENV)
        if not board:
            raise ValueError("RUL.011: name a board with board= or KOHAKUTPU_BOARD")
        self.line_bytes = MF.load(board).get("card.dram.line_bytes")
        # The card itself runs on every node its board has.
        whole = load_board(board)["meshes"] if kind == "card" else 1
        nodes = int(nodes or ENV.get(NODES_ENV) or whole)
        self.kind, self.nodes = kind, nodes
        if kind == "os":
            if os_session is None or os_units is None:
                raise ValueError(
                    "OS device requires a booted os_session and OS unit map"
                )
            if nodes != 1:
                raise ValueError("OS device adapter currently owns one node")
            self.build = None
            self.card = OSCard(board, os_session, os_units)
        elif kind in ("offline", "double"):
            self.build = None
            self.card = {"offline": OfflineCard, "double": DoubleCard}[kind](
                board,
                nodes=tuple(range(nodes)),
                resident_mode=self._resident_mode,
            )
        elif kind == "card":
            self.build = None
            self.card = SiliconCard(
                board,
                transport=transport or ENV.get(TRANSPORT_ENV, "jtag"),
                nodes=tuple(range(nodes)),
                profile=ENV.get(PROFILE_ENV, "idle"),
                resident_mode=self._resident_mode,
            )
        else:
            build = pathlib.Path(build or ENV.get(BUILD_ENV) or default_build(nodes))
            if not build.exists():
                raise FileNotFoundError(
                    f"no card model at {build}; build one (scripts/py/card_model.py) "
                    f"or set {BUILD_ENV}"
                )
            log = (
                log or pathlib.Path(tempfile.mkdtemp(prefix="ktpu_dev_")) / "model.log"
            )
            self.build = build
            self.card = VerilatorCard(
                build,
                board=board,
                nodes=tuple(range(nodes)),
                log=log,
                resident_mode=self._resident_mode,
            )
        self.machine = self.card.machine
        #: The card's memory, which every allocation here comes from.
        self.memory = self.card.memory
        spec = load_board(board)
        self.memory.package_homes = package_homes(
            ENV.get(PACKAGE_HOME_ENV, "near"), spec
        )
        if self.memory.package_homes:
            self.memory.stripes = (spec["dram_interleave_kb"] << 10, spec["dram_homes"])
        self.mmu = (
            rs._rs.NativeMMU()
            if self.memory.arena_cls is rs._rs.Arena
            else SoftwareMMU()
        )
        try:
            if ENV.get("KTPU_NATIVE_DIRECT", "0") == "1":
                native_direct.attach(self)
            for callback in ON_DEVICE_INIT:
                callback()
        except BaseException as error:
            try:
                self.card.close()
            except BaseException as close_error:  # noqa: BLE001
                error.add_note(f"close failed: {close_error!r}")
            raise
        self.closed = False
        self.dram = self.memory.dram
        #: Physical capacity, before queue/scratch reservations.
        self.dram_bytes = self.card.dram_bytes
        #: Compatibility view: recordings use only the main arenas.
        self.private: dict = {}
        self.recording = False
        self._record_pins: list = []
        self._record_runs: list = []
        self._record_templates: list = []
        self.runs: list = []
        #: Runs submitted and not yet collected, oldest first (`submit`).
        self.inflight: list = []
        self._uncertain: list = []
        #: The most runs held in flight (None: bounded by memory alone).
        self.max_inflight: int | None = depth
        capacity = sum(a.hi - a.lo for a in self.memory.arenas())
        self.queue_bytes = capacity if budget is None else budget
        self._scope = None
        self._placement = None
        #: The node of an `on_node` scope, None outside one.
        self.home: int | None = None
        #: (space, node) -> (address, bytes) of a scratch window (`_window`)
        self.windows: dict = {}
        self._failure = None
        self.library_only = library_only
        self.library = (
            str(library or ROOT / "build" / "templates")
            if library_only or library is not None
            else None
        )
        self.counters = {
            "runs": 0,
            "cycles": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "library_hits": 0,
            "class_hits": 0,
            "exact_hits": 0,
            "library_misses": 0,
            "compile_calls": 0,
            "compile_seconds": 0.0,
            "kernel_reuses": 0,
            # Mover commands F-001's split made, over the kernels compiled (L1.MOV.019).
            "f001_split": 0,
        }
        #: folded constants uploaded once, by content (`forms.table`)
        self.tables: dict = {}
        #: Their spans, which no run writes.
        self.constants: set = set()
        #: Runs on two nodes overlap when they share no written span. Off: on
        #: v9t6 overlapping nodes can hang or corrupt a run (ISSUES 31).
        self.node_overlap = ENV.get("KTPU_NODE_OVERLAP", "0") == "1"
        for node, queue in getattr(self.card, "queues", {}).items():
            queue.spill = partial(self._spill, node)

    @property
    def resident_mode(self) -> ResidentMode:
        if rs._MUTANT.get() == "app.device/unlatched_resident":
            return parse_resident_mode(ENV.get(RESIDENT_ENV))
        return self._resident_mode

    @resident_mode.setter
    def resident_mode(self, value: str | ResidentMode | None) -> None:
        mode = parse_resident_mode(value)
        self._resident_mode = mode
        self.card.resident_mode = mode

    @property
    def library(self) -> Any:
        return self._library

    @library.setter
    def library(self, value):
        if value != self._library:
            self._invalidate_eager()
        self._library = value

    @property
    def machine(self) -> Any:
        return self._machine

    @machine.setter
    def machine(self, value):
        if value is not self._machine:
            self._invalidate_eager()
        self._machine = value

    def close(self) -> dict:
        if self.closed:
            return {}
        try:
            self.drain()
        finally:
            # No reservation is revoked until card shutdown succeeds.
            result = self.card.close()
            if self.card.native_direct is not None:
                self.card.native_direct.close(shutdown=True)
            for queue in getattr(self.card, "queues", {}).values():
                if hasattr(queue, "discard_packages"):
                    queue.discard_packages()
            self.closed = True
            if self.eager_recipes is not None:
                self.eager_recipes.clear()
            for arena in self.memory.arenas():
                arena.close()
            self.inflight.clear()
            self._uncertain.clear()
            self.mmu.entries.clear()
        return result

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


@contextmanager
def attached(dev: Device | None = None) -> Iterator[Device | None]:
    """`dev` the process's device while the block runs (None: whichever it
    is), and the process's device as it was once the block ends, however it
    ends or whatever the block attached."""
    global _device
    keep = _device
    try:
        if dev is not None:
            _device = dev
        yield _device
    finally:
        _device = keep


__all__ = [
    "BOARD_ENV",
    "BUILD_ENV",
    "DEVICE_ENV",
    "MAX_INFLIGHT",
    "NODES_ENV",
    "TRANSPORTS",
    "TRANSPORT_ENV",
    "Device",
    "Recorded",
    "Run",
    "Submitted",
    "arena_name",
    "attach",
    "attached",
    "default_build",
    "device",
]
