"""Which machine the driver is pointed at, as DATA rather than as constants.

A bitstream is one mesh shape and one address map, and there will be several.
Everything that differs between them lives in a :class:`Board` -- cluster
coordinates, the two AXI windows, whether the quantise marker is reachable,
and who executes the control program.

COORDINATES ARE STORED, NOT DERIVED. :func:`ktpu.hw.bench.mesh` generates the
shape the BENCH builds; a bitstream is whatever it was synthesised with, and
the two drift apart without either being wrong -- they differ TODAY for
`split`. So a board carries the coordinates it was built with and
:meth:`Board.verify` reports the difference, rather than one side silently
winning and a flit going to a node that is not the one that was meant.
"""

import json
import os
import pathlib
from dataclasses import asdict, dataclass, field

from ktpu.hw import bench
from ktpu.hw import device as dev

# mag.v DATA_W: one device memory word, which is what bench.plan counts in.
MEM_WORD_BYTES = 32

ROOT = pathlib.Path(__file__).resolve().parents[3]
BOARDS = ROOT / "boards"
BOARD_ENV = "KTPU_BOARD"


def _coords(v):
    return tuple((int(x), int(y)) for x, y in v)


def _int(v):
    """An address from JSON, decimal or "0x..." -- a 34-bit map wants hex."""
    return int(v, 0) if isinstance(v, str) else int(v)


@dataclass(frozen=True)
class Board:
    """One machine: where its clusters are and where its windows live.

    ``clusters`` are DISPATCH coordinates in dispatch order -- the manager on a
    split bitstream, the single node on a merged one -- so everything
    downstream reads one list and needs no layout awareness. ``accumulators``
    is recorded for split boards because a node that is never dispatched to is
    still a node, and a picture without it is a picture of a smaller mesh.

    ``orchestrator`` is "host" when there is no `main_orch` on the bitstream,
    which is the case for every mesh :mod:`scripts.py.gen_mesh` emits: the
    control window goes straight to the agent and the host runs the program.
    """

    name: str
    layout: str
    clusters: tuple
    ports: tuple
    ncol: int
    nrow: int
    ctrl_base: int
    mem_base: int = 0
    orchestrator: str = "host"
    orc_base: int = 0
    # None means the memory window cannot reach the marker, so quantise-on-
    # upload is not merely unused -- it is unavailable, and asking must fail.
    quant_bit: int | None = None
    blay_bit: int | None = None
    stage_flits: int = bench.STAGE_FLITS
    inst_depth: int = dev.INST_DEPTH
    mem_words: int = bench.WORDS
    # Over-running one is SILENT. Measured: planning 128x64x128 for TILES=512 on
    # a TILES=256 bitstream put 15,440 of 16,384 elements past 10%, verdict green.
    tiles: int = bench.TILES
    l1_a: int = bench.L1_A_ENTRIES
    l1_b: int = bench.L1_B_ENTRIES
    l1_banks: int = bench.L1_BANKS
    mem_ports: int = 0  # 0 means "one per row", which is what mag.v builds
    accumulators: tuple = ()
    # noc_cu_base's CU_VERSION: a MESH-WIDE build number, bumped in every
    # endpoint whenever any ISA or datapath changes. The shipped card is 1.
    cu_version: int = 1
    # `ktpu.target.FEATURES` this bitstream implements. Empty is the safe
    # reading: a feature the machine predates is a wrong answer, not a slow one.
    features: tuple = ()
    transport: str = "xdma"
    source: str = ""
    vectors: tuple = field(default_factory=tuple)

    def __post_init__(self):
        for name in ("clusters", "ports", "accumulators", "vectors"):
            object.__setattr__(self, name, _coords(getattr(self, name)))
        object.__setattr__(self, "features", tuple(self.features))
        if self.layout not in bench.LAYOUTS:
            raise ValueError(
                f"unknown layout {self.layout!r}; use one of {list(bench.LAYOUTS)}"
            )
        if self.orchestrator not in ("host", "device"):
            raise ValueError(
                f"orchestrator is 'host' or 'device', not {self.orchestrator!r}"
            )

    @property
    def ncl(self) -> int:
        return len(self.clusters)

    @property
    def agent(self) -> tuple:
        """Where a reply flit is addressed. The agent answers at port 0.

        It has no node of its own -- mag.v s202 gives `noc_orchestrator`
        ORC_X/ORC_Y = the first memory port's coordinate, and the share layer
        hands it anything that is not a memory flit arriving there.
        """
        return self.ports[0]

    def target(self, clusters=None):
        """A :class:`ktpu.target.Target` describing THIS machine, for the DSL.

        The compiler needs the board's capacities for the same reason the
        planner does, and gets them wrong the same silent way: a tile chosen
        for capacities the bitstream does not have overruns the CU and the run
        reports success. `clusters` narrows it for a single-cluster lowering.

        `features` is empty unless the board says otherwise -- an optional
        feature the bitstream predates is a wrong answer, not a slow one.
        """
        from ktpu.target import Target

        return Target(
            name=self.name,
            clusters=self.ncl if clusters is None else clusters,
            vector_cores=len(self.vectors),
            tiles=self.tiles,
            l1_a=self.l1_a,
            l1_b=self.l1_b,
            l1_a_banks=self.l1_banks,
            features=frozenset(self.features),
        )

    def caps(self) -> bench.Capacities:
        """What one of this machine's clusters holds, for the planner."""
        return bench.Capacities(
            tiles=self.tiles,
            l1_a=self.l1_a,
            l1_b=self.l1_b,
            l1_banks=self.l1_banks,
            stage_flits=self.stage_flits,
        )

    # ---- the address map ------------------------------------------------
    def ctrl(self, off: int) -> int:
        """Byte address of an agent register at window offset `off`."""
        return self.ctrl_base + off

    def mem_byte(self, word: int) -> int:
        """Byte address of device memory word `word`, as bench.plan counts them."""
        return self.mem_base + word * MEM_WORD_BYTES

    def upload_addr(self, word: int, quant: bool = False, blayout: int = 0) -> int:
        """Where the host writes an operand region, marker bits included.

        AXI carries no field for "quantise this", so mag.v reads it off the top
        two address bits (mag.v s561). A board whose memory segment does not
        span them cannot ask, and must not pretend to: an FP16 image landing in
        a region sized for MXFP7 overruns the operand after it, and the run
        answers with no error at all.
        """
        addr = self.mem_byte(word)
        if not quant:
            return addr
        if self.quant_bit is None:
            raise ValueError(
                f"board {self.name!r} cannot mark an upload for quantisation -- "
                "its memory window does not reach the marker bit. Upload with "
                "preq=False, or widen the S_AXI_MEM segment to 16G so the host "
                "can present address bits 33:32."
            )
        addr |= 1 << self.quant_bit
        if blayout:
            addr |= 1 << self.blay_bit
        return addr

    def rebase(self, addr: int) -> int:
        """A bench-space address -- ORC_BASE / MAG_BASE -- in this board's map.

        The programs the planner builds address the bench, where bit 28 selects
        MAG. Translating here rather than parameterising `Program` keeps one
        program shape for both and leaves the simulation path byte-identical.
        """
        if addr & dev.MAG_BASE:
            return self.ctrl_base + (addr & (dev.MAG_BASE - 1))
        if self.orchestrator != "device":
            raise ValueError(
                f"{addr:#x} is a main_orch register and board {self.name!r} has no "
                "main_orch; its control program runs on the host"
            )
        return self.orc_base + addr

    def one_window(self, addr: int, nbytes: int) -> None:
        """Refuse a block that straddles ORC and MAG: the map is affine per window."""
        if (addr & dev.MAG_BASE) != ((addr + nbytes - 1) & dev.MAG_BASE):
            raise ValueError(
                f"a {nbytes}-byte block at {addr:#x} crosses the ORC/MAG boundary"
            )

    def view(self, inner: dev.Transport) -> dev.Transport:
        """`inner`, with bench addresses translated into this board's map."""
        return Rebased(self, inner)

    # ---- provenance -----------------------------------------------------
    def verify(self) -> list[str]:
        """How this board differs from the mesh `bench` would build. Empty is agreement.

        Reported, never resolved. A generator that has moved on is not evidence
        that the card has, and the card is the thing the flits arrive at.
        """
        try:
            m = bench.mesh(self.ncl, self.layout)
        except ValueError as exc:
            return [str(exc)]
        why = []
        for what, mine, theirs in (
            (
                "cluster coordinates",
                self.clusters,
                _coords(c["cu"] for c in m["clusters"]),
            ),
            ("memory ports", self.ports, _coords(m["ports"])),
            ("grid", (self.ncol, self.nrow), (m["ncol"], m["nrow"])),
        ):
            if mine != theirs:
                why.append(f"{what}: board {list(mine)}, bench.mesh {list(theirs)}")
        # The capacities differ far more often than the coordinates, because the
        # RTL's move while a bitstream's are frozen at synthesis.
        for what, mine, theirs in (
            ("TILES", self.tiles, bench.TILES),
            ("L1 A entries", self.l1_a, bench.L1_A_ENTRIES),
            ("L1 B entries", self.l1_b, bench.L1_B_ENTRIES),
            ("stage flits", self.stage_flits, bench.STAGE_FLITS),
        ):
            if mine != theirs:
                why.append(f"{what}: board {mine}, bench {theirs}")
        return why

    # ---- files ----------------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("clusters", "ports", "accumulators", "vectors"):
            d[k] = [list(c) for c in d[k]]
        d["features"] = list(d["features"])
        for k in ("ctrl_base", "mem_base", "orc_base"):
            d[k] = hex(d[k])
        return d

    def save(self, path) -> pathlib.Path:
        path = pathlib.Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, d: dict) -> "Board":
        d = dict(d)
        for k in ("ctrl_base", "mem_base", "orc_base"):
            if k in d:
                d[k] = _int(d[k])
        return cls(**d)

    @classmethod
    def load(cls, path) -> "Board":
        return cls.from_dict(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))

    @classmethod
    def named(cls, name: str) -> "Board":
        """A board out of ``boards/``, by file stem."""
        path = BOARDS / f"{name}.json"
        if not path.exists():
            have = (
                sorted(p.stem for p in BOARDS.glob("*.json")) if BOARDS.exists() else []
            )
            raise FileNotFoundError(f"no board {name!r} in {BOARDS}; have {have}")
        return cls.load(path)

    @classmethod
    def select(cls, spec=None) -> "Board | None":
        """A board from `spec` or ``$KTPU_BOARD``; a name, a .json path, or None.

        Returns None when nothing names one, so a caller can say what it needs
        in its own words rather than raising a generic KeyError from here.
        """
        spec = spec or os.environ.get(BOARD_ENV)
        if spec is None:
            return None
        if isinstance(spec, cls):
            return spec
        path = pathlib.Path(spec)
        return cls.load(path) if path.suffix == ".json" else cls.named(str(spec))

    @classmethod
    def from_mesh(cls, name: str, ncl: int, layout: str = "merged", **kw) -> "Board":
        """A board whose shape is whatever `bench.mesh` builds. For simulation.

        The bench IS its own generator, so here derivation is the truth rather
        than a guess -- which is exactly why a synthesised board does not use it.
        """
        m = bench.mesh(ncl, layout)
        return cls(
            name=name,
            layout=layout,
            clusters=[c["cu"] for c in m["clusters"]],
            accumulators=[c["acu"] for c in m["clusters"] if "acu" in c],
            ports=m["ports"],
            ncol=m["ncol"],
            nrow=m["nrow"],
            **kw,
        )


def bench_board(ncl: int = 2, layout: str | None = None) -> Board:
    """The machine `mag_driver_tb` builds: main_orch present, markers reachable."""
    return Board.from_mesh(
        "bench",
        ncl,
        layout or bench.LAYOUT,
        ctrl_base=dev.MAG_BASE,
        orc_base=dev.ORC_BASE,
        orchestrator="device",
        quant_bit=33,
        blay_bit=32,
        transport="record",
        source="ktpu.hw.bench.mesh -- the bench generates this shape itself",
    )


class Rebased(dev.Transport):
    """A transport whose addresses are bench addresses, translated on the way out.

    Not a cache and not a buffer: every access reaches `inner`, at a different
    number. `bulk` follows the inner transport, because whether coalescing pays
    is a property of the wire and not of the arithmetic above it.
    """

    def __init__(self, board: Board, inner: dev.Transport):
        self.board = board
        self.inner = inner
        self.bulk = inner.bulk

    def write64(self, addr: int, data: int) -> None:
        self.inner.write64(self.board.rebase(addr), data)

    def read64(self, addr: int) -> int:
        return self.inner.read64(self.board.rebase(addr))

    def write_block(self, addr: int, data: bytes) -> None:
        self.board.one_window(addr, len(data))
        self.inner.write_block(self.board.rebase(addr), data)

    def read_block(self, addr: int, nbytes: int) -> bytes:
        self.board.one_window(addr, nbytes)
        return self.inner.read_block(self.board.rebase(addr), nbytes)

    def __repr__(self):
        return f"Rebased({self.board.name}, {self.inner!r})"


def open_transport(
    spec: str | None = None, board: Board | None = None
) -> dev.Transport:
    """Open a backend by name, rebased onto `board` when one is given.

    ``record``, ``memory``, ``xdma``, ``xdma:1`` for a second card, ``jtag``.
    Falls back to ``$KTPU_TRANSPORT`` and then to the board's own choice.
    Raises :class:`~ktpu.hw.device.TransportUnavailable` when the named backend
    cannot be reached -- never a bare OSError, so a caller can explain itself.

    The backend modules are imported HERE, one at a time: a driver that
    imports every backend makes every caller depend on every one of them.
    """
    spec = (
        spec
        or os.environ.get("KTPU_TRANSPORT")
        or (board.transport if board else "record")
    )
    kind, _, arg = spec.partition(":")
    if kind == "record":
        inner = dev.RecordingTransport()
    elif kind == "memory":
        inner = dev.MemoryTransport()
    elif kind == "xdma":
        from ktpu.hw.xdma import XdmaTransport

        inner = XdmaTransport(index=int(arg or 0))
    elif kind == "jtag":
        from ktpu.hw.jtag import JtagTransport

        inner = JtagTransport()
    else:
        raise dev.TransportUnavailable(
            f"unknown transport {spec!r}; use record, memory, xdma[:n] or jtag"
        )
    return board.view(inner) if board is not None else inner
