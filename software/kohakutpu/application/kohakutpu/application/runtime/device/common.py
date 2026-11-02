"""What the device's parts share: environment names, recorded and submitted runs."""

import pathlib
from dataclasses import dataclass, field
from typing import Any

from kohakutpu.application.runtime.paths import ROOT

DEVICE_ENV = "KOHAKUTPU_DEVICE"

BUILD_ENV = "KOHAKUTPU_SIM_BUILD"

#: How many nodes (sysnodes sharing memory through the Xache) the card has.
NODES_ENV = "KOHAKUTPU_SIM_NODES"

#: The required board a device opens (boards/<name>.json).
BOARD_ENV = "KOHAKUTPU_BOARD"

ON_DEVICE_INIT: list = []

#: `sim` Verilated, `card` the card, `offline` compiled, checked and encoded
#: with nothing executed, `double` offline with L2 on the value simulator.
KINDS = ("sim", "card", "offline", "os", "double")

#: How a `card` device reaches the card (`card.TRANSPORTS`).
TRANSPORT_ENV = "KOHAKUTPU_TRANSPORT"

#: The board clock profile a `card` device requires (`idle`: the board's idle one).
PROFILE_ENV = "KOHAKUTPU_PROFILE"

#: The card models' build root: fast models (no probes) with V2 vector cores
#: and DRAM sized by the named machine's card.dram.bytes.
SIM_ROOT = "v9g"


def default_build(nodes: int) -> pathlib.Path:
    return ROOT / "build" / SIM_ROOT / f"vlt_card_v9_{nodes}n_v2"


DEFAULT_BUILD = default_build(1)

#: Compatibility value for RUL.020 observers; no allocation adds a tail.
TAIL = 0

#: Optional extra host limit; hardware limits come from each queue's layout.
MAX_INFLIGHT = None

#: Compiled kernels a device keeps by graph text (`Device.kernel`).
KERNELS_KEPT = 4096

QUEUE_ENV = "KOHAKUTPU_QUEUE"

QUEUE_BYTES_ENV = "KOHAKUTPU_QUEUE_BYTES"

QUEUE_DEPTH_ENV = "KOHAKUTPU_QUEUE_DEPTH"

PREPARED_REPLAY_ENV = "KOHAKUTPU_PREPARED_REPLAY"

REPLAY_PACKAGE_BYTES_ENV = "KOHAKUTPU_REPLAY_PACKAGE_BYTES"

#: Where packages go: `any`, `near` or a home per node (`memory.package_homes`).
PACKAGE_HOME_ENV = "KOHAKUTPU_PACKAGE_HOME"


class Parts:
    """What `Device`'s parts read of one another, declared only: `Device.__init__` or a
    sibling part defines each."""

    card: Any
    memory: Any
    mmu: Any
    dram: Any
    machine: Any
    library: Any
    resident_mode: Any
    home: int | None
    line_bytes: int
    closed: bool
    queued: bool
    recording: bool
    eager_flush: bool
    prepared_replay: bool
    replay_overcommit: bool
    replay_package_bytes: int | None
    replay_package_fraction: float
    queue_bytes: int
    max_inflight: int | None
    counters: dict
    kernels: Any
    runs: list
    inflight: list
    private: dict
    windows: dict
    _uncertain: list
    _failure: Any
    _placement: Any
    _scope: Any
    _record_pins: list
    _record_runs: list
    _record_templates: list
    _recipe_capture: Any
    _recipe_recording: Any
    _admit: Any
    _extents: Any
    _fence: Any
    _healthy: Any
    _kernel_key: Any
    _pin_record: Any
    _release_windows: Any
    _retain: Any
    _windowed: Any
    collect: Any
    collect_through: Any
    drain: Any
    free: Any
    wait_address: Any


class Recorded(list):
    """A replay log owning references to every allocation its events use."""

    def __init__(self, events, references, owner, runs=(), templates=()):
        super().__init__(events)
        self.references = references
        self.runs = list(runs)
        self.templates = tuple(templates)
        self.owner = owner
        self.closed = False
        self.prepared: Any = None
        self.list_events: list | None = None
        self.list_runs: Any = None
        self.native: Any = None

    def close(self):
        if not self.closed:
            if self.native is not None:
                self.native.close()
                self.native = None
            if self.prepared is not None:
                self.prepared.close()
                self.prepared = None
            for arena, at in self.references:
                arena.unretain(at)
            self.references = []
            self.closed = True

    def __del__(self):
        self.close()


@dataclass
class Submitted:
    """A kernel run queued on the card (`Device.submit`): the spans it
    references (its scratch windows among them), held until it is collected,
    the card's pending run or result, and for a recorded run each window's
    address with the bytes this run lays out in it (`_extents`)."""

    name: str
    kind: str
    info: dict
    t0: float
    pending: object = None
    result: dict | None = None
    run: "Run | None" = None
    references: list = field(default_factory=list)
    writes: list = field(default_factory=list)
    nodes: tuple = ()
    error: Exception | None = None
    extents: dict = field(default_factory=dict)


@dataclass
class Run:
    """One kernel run: its name, the card's cycles, the wall time."""

    name: str
    cycles: int
    wall: float
    kind: str = "dsl"
    info: dict = field(default_factory=dict)
