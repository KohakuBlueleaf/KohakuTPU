"""Level 2: the schedule IR.

Engine choice, fusion, tile, grid and residency. Level 1 is what the user meant,
level 3 is bytes; every decision is here, and every decision is an object that
prints.
"""

import itertools
from dataclasses import dataclass, field
from enum import Enum

from ktpu.ir.dtype import ACC24, E8M15, MXFP7, DType, Kind
from ktpu.ir.graph import OpKind


class Engine(Enum):
    """Where a band runs.

    HOST means NEITHER compute core can do it: index arithmetic, gather and
    scatter with computed indices, sort and top-k, and any data movement that is
    not a strided view. Such a band is legal and is lowered like any other, but
    it needs host support today and is what a future general or manager core
    would take over. Marking it is the point -- a kernel that silently assumed
    hardware support would be found out on the machine instead of here.
    """

    MATMUL = "matmul"
    VECTOR = "vector"
    HOST = "host"

    @property
    def on_device(self) -> bool:
        return self is not Engine.HOST

    @property
    def compute_dtype(self) -> DType:
        return MXFP7 if self is Engine.MATMUL else E8M15

    @property
    def accum_dtype(self) -> DType:
        return ACC24 if self is Engine.MATMUL else E8M15


class Space(Enum):
    DRAM = "dram"
    L1 = "l1"
    REG = "reg"


class ScheduleError(ValueError):
    """A schedule that cannot be lowered."""


@dataclass(frozen=True)
class Tile:
    """Output shape one grid instance computes. `k` is the chunk swept per GEMM."""

    m: int
    n: int = 1
    k: int = 1

    def __post_init__(self) -> None:
        if min(self.m, self.n, self.k) < 1:
            raise ScheduleError(f"tile extents must be positive, got {self}")

    def __str__(self) -> str:
        return f"{self.m}x{self.k}x{self.n}"


@dataclass(frozen=True)
class Grid:
    """Instance counts over (M, N, K-split).

    `sk > 1` makes instances sharing (mo, no) produce partials, which need a
    reduction band -- see `Schedule.verify`.
    """

    m: int
    n: int = 1
    sk: int = 1

    def __post_init__(self) -> None:
        if min(self.m, self.n, self.sk) < 1:
            raise ScheduleError(f"grid extents must be positive, got {self}")

    @property
    def size(self) -> int:
        return self.m * self.n * self.sk

    def instances(self):
        return itertools.product(range(self.m), range(self.n), range(self.sk))

    def covers(self, out_m: int, out_n: int, tile: Tile) -> None:
        """Assert every output element is produced exactly once.

        A gap leaves the output's initial contents in place; an overlap races.
        Overshoot below one tile is padding and is fine.
        """
        if self.m * tile.m < out_m:
            raise ScheduleError(
                f"grid leaves {out_m - self.m * tile.m} rows uncovered: "
                f"{self.m} x {tile.m} < {out_m}"
            )
        if self.n * tile.n < out_n:
            raise ScheduleError(
                f"grid leaves {out_n - self.n * tile.n} columns uncovered: "
                f"{self.n} x {tile.n} < {out_n}"
            )
        if (self.m - 1) * tile.m >= out_m:
            raise ScheduleError(
                f"{self.m} instances of {tile.m} rows overshoot {out_m} by a "
                "whole tile: some instance has no work"
            )
        if (self.n - 1) * tile.n >= out_n:
            raise ScheduleError(
                f"{self.n} instances of {tile.n} columns overshoot {out_n} by a "
                "whole tile: some instance has no work"
            )

    def __str__(self) -> str:
        return f"({self.m}, {self.n}, sk={self.sk})"


ENGINE_TAG = {"matmul": "m_", "vector": "v_", "host": "h_"}


@dataclass
class SchedOp:
    """One placed op. `engine` is where it runs, which is not always its band's.

    A folded epilogue lives in a MATMUL band but executes on the vector core, so
    the op carries its own engine and prints with that prefix.

    `ins` and `out` are LEVEL-1 VALUE IDS and they are not decoration: without
    them `mul(x, x)` and `mul(prev, x)` are the same object, and codegen emits
    both as a chained multiply -- so the second one silently squares the wrong
    value. Everything an operand can be (a chain step, a register, a broadcast
    scalar, a second load) is decided from these.
    """

    kind: OpKind
    attrs: dict = field(default_factory=dict)
    engine: Engine | None = None
    ins: tuple[int, ...] = ()
    out: int = -1
    #: Element offset into each input's ROOT tensor, from the slices folded
    #: into its address. A block loop reads the same region at a stride.
    offs: tuple[int, ...] = ()
    #: Element stride per input. 1 is contiguous; a single-column slice of an
    #: `(T, E)` gate is one value per row, so stride E.
    strides: tuple[int, ...] = ()

    @property
    def tag(self) -> str:
        return ENGINE_TAG.get(self.engine.value, "") if self.engine else ""

    def __str__(self) -> str:
        extra = "".join(f" {k}={v!r}" for k, v in sorted(self.attrs.items()))
        args = ", ".join(f"%{v}" for v in self.ins)
        head = f"%{self.out} = " if self.out >= 0 else ""
        return f"{head}{self.tag}{self.kind.value}({args}){extra}"


@dataclass
class Band:
    """A grid of instances over one engine.

    `residency` is what stays in L1 across the grid rather than refetching per
    instance. `reduces` names the band whose partials this one sums.
    """

    engine: Engine
    grid: Grid
    tile: Tile
    ops: list[SchedOp] = field(default_factory=list)
    residency: dict[str, bool] = field(default_factory=dict)
    space: dict[str, Space] = field(default_factory=dict)
    reduces: str | None = None
    #: Band whose output this one reads while still resident, off the mesh
    #: rather than from DRAM. Its grid is its own -- see `Schedule.verify`.
    consumes: str | None = None
    #: Every value the ops read but no op here produces, as vid -> (what, which):
    #: ("band", name) for another band's output, ("input", name) for a graph
    #: input, ("const", float) for a literal. Codegen has to bind each one to
    #: something the ISA can name, so a missing entry is an operand that does
    #: not exist on the machine.
    ext: dict[int, tuple[str, object]] = field(default_factory=dict)
    #: Element count of every value in `ext` and of every value the ops
    #: produce. An operand smaller than the tile is a broadcast, and which axis
    #: it broadcasts along is decided from this.
    elems: dict[int, int] = field(default_factory=dict)

    #: Logical shape of the widest value this band touches -- the INPUT for a
    #: reduction. Decides row versus column broadcast, per band not per program.
    shape: tuple[int, ...] = ()
    name: str = ""

    @property
    def produces_partials(self) -> bool:
        return self.grid.sk > 1

    def engines_used(self) -> list[Engine]:
        """Every engine this band's ops touch, band engine first."""
        seen = [self.engine]
        for o in self.ops:
            if o.engine and o.engine not in seen:
                seen.append(o.engine)
        return seen

    def __str__(self) -> str:
        """One line per engine, describing what ONE grid instance does."""
        tail = f"  reduces={self.reduces}" if self.reduces else ""
        if self.consumes:
            tail += f"  consumes={self.consumes}"
        head = (
            f"band {self.name or '?'}  grid{self.grid} = {self.grid.size} "
            f"instances{tail}"
        )
        lines = [head]

        mm = [o for o in self.ops if o.kind is OpKind.MATMUL]
        if mm:
            a = mm[0].attrs
            k, tk = a.get("k", 0), self.tile.k
            chunks = max(1, k // tk) if tk else 1
            lines.append(
                f"  m_ per instance  C[{self.tile.m},{self.tile.n}] += "
                f"A[{self.tile.m},{tk}] @ B[{tk},{self.tile.n}]"
                f"   x {chunks} K-chunks of {tk}  (K={k})"
            )
            lines.append(
                f"     whole problem  {a.get('m')} x {k} x {a.get('n')}"
                f"   accumulates in {Engine.MATMUL.accum_dtype}"
            )

        vec = [o for o in self.ops if o.engine is Engine.VECTOR]
        if vec:
            src = (
                f"{Engine.MATMUL.accum_dtype} resident from {self.consumes}"
                if self.consumes
                else "fp16 from DRAM"
            )
            lines.append(
                f"  v_ per instance  {len(vec)} ops on {self.tile.m}x{self.tile.n}"
                f" = {self.tile.m * self.tile.n} elements   reads {src}"
            )
            lines.append(
                f"     converts to {Engine.VECTOR.compute_dtype} on load, "
                f"stores fp16   {' '.join(o.kind.value for o in vec)}"
            )
        return "\n".join(lines)


def correspondence(producer: Band, consumer: Band) -> tuple[int, int]:
    """How many consumer instances share one producer tile, as `(sm, sn)` factors.

    A band reading another's output while it is still resident must walk the
    SAME tiles the producer wrote, so its grid has to be the producer's grid
    refined by an integer factor per axis and its tile the matching fraction of
    the producer's. `(1, 1)` is one consumer instance per producer tile; `(2, 1)`
    is two, each taking half the rows.

    Equal element TOTALS are not a correspondence and must not be accepted as
    one: they agree for any two grids covering the same output, which is how a
    flat 64 x 1024 epilogue passed for a partner of 8 tiles of 64 x 128 while
    naming none of them.

    Raises ScheduleError if the two grids admit no such factor pair.
    """
    p, c = producer.grid, consumer.grid
    pt, ct = producer.tile, consumer.tile
    if c.sk != 1:
        raise ScheduleError(
            f"band {consumer.name} consumes {producer.name} and splits K "
            f"{c.sk} ways; a consumer of a resident tile runs once over it"
        )
    if c.m % p.m or c.n % p.n:
        raise ScheduleError(
            f"band {consumer.name} grid{c} does not refine {producer.name}'s "
            f"grid{p}: {c.m}/{p.m} and {c.n}/{p.n} must both be whole"
        )
    sm, sn = c.m // p.m, c.n // p.n
    if ct.m * sm != pt.m or ct.n * sn != pt.n:
        raise ScheduleError(
            f"band {consumer.name} tile {ct.m}x{ct.n} x ({sm}, {sn}) instances "
            f"is not {producer.name}'s tile {pt.m}x{pt.n}; the grids cover the "
            "same element count but instance j names no producer tile"
        )
    return sm, sn


@dataclass
class Schedule:
    bands: list[Band] = field(default_factory=list)
    name: str = "schedule"

    #: Level-1 value ids the graph returns. Empty means the last band's output,
    #: the only one that is structurally recoverable.
    outputs: list[int] = field(default_factory=list)

    def add(self, band: Band) -> Band:
        if not band.name:
            band.name = f"b{len(self.bands)}"
        self.bands.append(band)
        return band

    def verify(self) -> None:
        names = [b.name for b in self.bands]
        if len(set(names)) != len(names):
            raise ScheduleError(f"duplicate band names in {names}")

        for b in self.bands:
            if b.engine is Engine.MATMUL and b.grid.sk > 1:
                reducers = [r for r in self.bands if r.reduces == b.name]
                if not reducers:
                    raise ScheduleError(
                        f"band {b.name} splits K {b.grid.sk} ways but nothing "
                        "reduces its partials; a split-K band needs a matching "
                        "reduction band on the vector core"
                    )
                for r in reducers:
                    if r.engine is not Engine.VECTOR:
                        raise ScheduleError(
                            f"band {r.name} reduces partials on "
                            f"{r.engine.value}; that must be the vector core, "
                            "which is the only engine with the range for it"
                        )
            if b.reduces is not None and b.reduces not in names:
                raise ScheduleError(f"band {b.name} reduces unknown band {b.reduces!r}")

        for i, b in enumerate(self.bands):
            if b.consumes is None:
                continue
            if b.consumes not in names:
                raise ScheduleError(
                    f"band {b.name} consumes unknown band {b.consumes!r}"
                )
            j = names.index(b.consumes)
            if j >= i:
                raise ScheduleError(
                    f"band {b.name} consumes {b.consumes}, which runs after it; "
                    "nothing is resident yet"
                )
            p = self.bands[j]
            if p.engine is not Engine.MATMUL:
                raise ScheduleError(
                    f"band {b.name} consumes {p.name}, which runs on "
                    f"{p.engine.value}; only a cluster holds a tile a later band "
                    "can read without going through DRAM"
                )
            if b.engine is not Engine.VECTOR:
                raise ScheduleError(
                    f"band {b.name} runs on {b.engine.value} and consumes a "
                    "resident matmul tile; only the vector core can read one"
                )
            if p.grid.sk > 1:
                raise ScheduleError(
                    f"band {b.name} consumes {p.name}, which splits K "
                    f"{p.grid.sk} ways: its tiles are PARTIALS, so the band that "
                    "reads them reduces= them rather than consumes= them"
                )
            correspondence(p, b)

    def host_bands(self) -> list[Band]:
        """Bands neither compute core can run, so the host must.

        Empty is the goal. A non-empty result names exactly what a general or
        manager core would need to absorb.
        """
        return [b for b in self.bands if b.engine is Engine.HOST]

    def __str__(self) -> str:
        return f"schedule {self.name}\n" + "\n".join(str(b) for b in self.bands)


def split_k_needed(k: int, peak_estimate: float, out_dtype: DType) -> bool:
    """Must this reduction finish somewhere wider than `out_dtype`?

    Over biased operands a dot product grows linearly in K. ACC24 holds it;
    the FP16 store is what saturates (mx_fpacc.v:582).
    """
    del k
    if out_dtype.kind is not Kind.FLOAT:
        return False
    return peak_estimate > out_dtype.max_finite
