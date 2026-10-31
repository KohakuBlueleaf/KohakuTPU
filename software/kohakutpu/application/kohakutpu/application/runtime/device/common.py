"""What the device's parts share: environment names, recorded and submitted runs."""

import pathlib
from dataclasses import dataclass, field

from kohakutpu.application.runtime.paths import ROOT

DEVICE_ENV = "KOHAKUTPU_DEVICE"

BUILD_ENV = "KOHAKUTPU_SIM_BUILD"

#: How many nodes (sysnodes sharing memory through the Xache) the card has.
NODES_ENV = "KOHAKUTPU_SIM_NODES"

#: The required board a device opens (boards/<name>.json).
BOARD_ENV = "KOHAKUTPU_BOARD"

ON_DEVICE_INIT: list = []

#: `sim` the Verilated card, `card` the card itself, `offline` nothing
#: attached (every run compiled, checked and encoded; nothing executed).
KINDS = ("sim", "card", "offline", "os")

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


class Recorded(list):
    """A replay log owning references to every allocation its events use."""

    def __init__(self, events, references, owner, runs=(), templates=()):
        super().__init__(events)
        self.references = references
        self.runs = list(runs)
        self.templates = tuple(templates)
        self.owner = owner
        self.closed = False
        self.prepared = None
        self.list_events = self.list_runs = None
        self.native = None

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
