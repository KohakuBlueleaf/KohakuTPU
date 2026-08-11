"""Binding a band's operands to things the vector ISA can name.

A `SchedOp` names its operands by level-1 value id. The machine has vector
registers, scalar registers, a chain wire between adjacent ALUs, and loads. This
decides which of those each operand is, and what has to be spilled because a
chain deeper than one VMODE pass runs as several passes over all the data and
nothing in the register file survives between them.
"""

from dataclasses import dataclass, field

from ktpu.ir.graph import REDUCTION, OpKind
from ktpu.ir.sched import Band, SchedOp

VREGS = 16
SREGS = 16


class BindError(RuntimeError):
    """A band whose operands cannot be placed on the machine."""


@dataclass(frozen=True)
class Slot:
    """Where one operand is read from.

    `chain` is the previous ALU's result, `vreg`/`sreg` are register files,
    `tile` is the band's own input tile, `tensor` a separately loaded operand,
    `spill` a value parked in `tmp` by an earlier micro-job.
    """

    kind: str
    idx: int = 0
    name: str = ""

    def __str__(self) -> str:
        match self.kind:
            case "chain":
                return "C"
            case "vreg":
                return f"v{self.idx}"
            case "sreg":
                return f"s{self.idx}"
            case "tile":
                return "tile"
            case _:
                return f"{self.kind}:{self.name}"


@dataclass
class Job:
    """One VMODE pass over every chunk the core owns."""

    ops: list[SchedOp]
    loads: list[tuple[int, Slot, int]] = field(default_factory=list)
    stores: list[tuple[int, int, int]] = field(default_factory=list)
    exports: list[tuple[int, int]] = field(default_factory=list)
    binds: list[tuple[SchedOp, list[Slot], Slot]] = field(default_factory=list)


@dataclass
class Plan:
    """A vector band's operands, resolved into ONE hardware-loop body.

    The whole chain runs per VL chunk: `VMODE` bounds how many ops CHAIN --
    bypass the register file between adjacent ALUs -- and a value that outlives
    its group lives in a register, which is what registers are for. Only state
    that crosses CHUNKS needs L1, and elementwise chains have none.
    """

    jobs: list[Job]
    consts: dict[int, float]
    spills: dict[int, int]
    tensors: dict[int, str]
    source: int = -1
    peak: int = 0

    @property
    def nspill(self) -> int:
        return len(self.spills)


def _allocator(free: list[int], reg: dict[int, int], band: str, job: int):
    """A function that hands out one of `free` to a value and records it in
    `reg`. Raises BindError when the file is exhausted."""

    def alloc(v: int) -> int:
        if not free:
            raise BindError(
                f"band {band} micro-job {job} needs more than {VREGS} live "
                "vector registers; reduce the chain depth"
            )
        reg[v] = free.pop(0)
        return reg[v]

    return alloc


def _uses(ops: list[SchedOp]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for o in ops:
        for v in o.ins:
            counts[v] = counts.get(v, 0) + 1
    return counts


def plan(band: Band, depth: int, exports: frozenset[int] = frozenset()) -> Plan:
    """Resolve every operand of `band`, splitting its ops into micro-jobs of
    `depth`.

    Constants become scalar registers, set once per core. The band's own input
    tile is `Slot("tile")`. Any other external value is a `tensor` operand and
    gets its own load. A value produced in one micro-job and read in a later one
    is a `spill`: it round-trips through `tmp`, because a job loops over all of
    the core's chunks and the register file does not survive that.

    `exports` are values a LATER band reads, which must be stored to their own
    region and so cannot live on the chain wire.

    Returns a Plan. Raises BindError if a job needs more than `VREGS` live
    registers, if constants exceed `SREGS`, or if an operand resolves to
    nothing -- each of which means the band cannot run as written.
    """
    ops = [o for o in band.ops if o.kind is not OpKind.MATMUL]
    made = {o.out for o in ops}

    consts = {v: float(w) for v, (k, w) in band.ext.items() if k == "const"}
    tensors = {v: str(w) for v, (k, w) in band.ext.items() if k in ("input", "value")}
    made_by = {v: str(w) for v, (k, w) in band.ext.items() if k == "band"}

    full = band.grid.size * band.tile.m * band.tile.n
    source = next((v for v, n in made_by.items() if n == band.consumes), -1)
    if source < 0:
        # A windowed operand cannot be the tile source: the tile path reads a
        # contiguous span and the window lives in the load's address.
        whole = [
            v
            for v in (*made_by, *tensors)
            if band.elems.get(v, full) == full and v not in band.windows
        ]
        source = min(whole) if whole else -1
    for v, n in made_by.items():
        if v != source:
            tensors[v] = f"{n}:%{v}"
    tensors.pop(source, None)
    if len(consts) > SREGS:
        raise BindError(
            f"band {band.name} needs {len(consts)} scalar registers and the "
            f"core has {SREGS}; fold constants or split the band"
        )
    sreg = {v: i for i, v in enumerate(sorted(consts))}

    total = _uses(ops)
    last_use: dict[int, int] = {}
    for i, o in enumerate(ops):
        for v in o.ins:
            last_use[v] = i

    job = Job(ops=ops)
    reg: dict[int, int] = {}
    free = list(range(VREGS))
    alloc = _allocator(free, reg, band.name, 0)

    for v in sorted({v for o in ops for v in o.ins} - made - set(consts)):
        if v == source:
            job.loads.append((v, Slot("tile"), alloc(v)))
        elif v in tensors:
            job.loads.append((v, Slot("tensor", 0, tensors[v]), alloc(v)))
        else:
            raise BindError(
                f"band {band.name} reads %{v} and nothing produces or names it"
            )

    peak = len(reg)
    on_chain = -1
    nscalar = len(consts)
    for i, o in enumerate(ops):
        srcs = []
        for v in o.ins:
            if v in consts or v in sreg:
                srcs.append(Slot("sreg", sreg[v], f"{consts.get(v, '')}"))
            elif v == on_chain:
                srcs.append(Slot("chain"))
            elif v in reg:
                srcs.append(Slot("vreg", reg[v]))
            else:
                raise BindError(f"band {band.name}: %{v} is not live at %{o.out}")
        if o.kind in REDUCTION:
            if nscalar >= SREGS:
                raise BindError(
                    f"band {band.name} needs more than {SREGS} scalar registers"
                )
            sreg[o.out] = nscalar
            nscalar += 1
            dst, chained = Slot("sreg", sreg[o.out], f"%{o.out}"), False
        else:
            chained = (
                i + 1 < len(ops)
                and (i + 1) // depth == i // depth
                and ops[i + 1].ins.count(o.out) == 1
                and total.get(o.out, 0) == 1
                and o.out not in exports
            )
            dst = Slot("chain") if chained else Slot("vreg", alloc(o.out))
        on_chain = o.out if chained else -1
        job.binds.append((o, srcs, dst))
        peak = max(peak, len(reg))
        for v in o.ins:
            if v in reg and last_use.get(v) == i and v not in exports:
                free.append(reg.pop(v))

    dsts = {o.out: d for o, _, d in job.binds}
    for o in ops:
        if o.out in exports:
            job.exports.append((o.out, dsts[o.out].idx))

    return Plan(
        jobs=[job],
        consts=consts,
        spills={},
        tensors=tensors,
        source=source,
        peak=peak,
    )
