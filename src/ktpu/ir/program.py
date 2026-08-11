"""Level 3: the target program. Concrete instructions with resolved addresses.

One instruction stream over every engine, plus the memory map they address into.
No decisions are made here; level 2 made them all.

Instruction sets: docs/isa/cluster.md for the cluster, docs/isa/vector.md for
the vector core. Field names below follow those documents. The exact bit
POSITIONS of a packed flit are not yet aligned with the legacy encoder, so this
carries symbolic fields rather than a `words()` that would look authoritative
and be wrong.
"""

from dataclasses import dataclass, field
from enum import Enum

from ktpu.ir.sched import Engine


class Opcode(Enum):
    FILL = "fill"
    GEMM = "gemm"
    DRAIN = "drain"

    VSETVL = "vsetvl"
    VSETMD = "vsetmd"
    VSETI = "vseti"
    VLOOP = "vloop"
    VLD = "vld"
    VST = "vst"
    VALU = "valu"
    VRED = "vred"

    HOST = "host"


@dataclass(frozen=True)
class Inst:
    """One instruction for one endpoint.

    `node` is the destination as (x, y) in that engine's own coordinate space;
    `engine` says which space. `fields` carries the operands the ISA names.
    """

    op: Opcode
    engine: Engine
    node: tuple[int, int]
    fields: dict = field(default_factory=dict)

    def __str__(self) -> str:
        args = " ".join(f"{k}={v}" for k, v in self.fields.items())
        x, y = self.node
        tag = {Engine.MATMUL: "cu", Engine.VECTOR: "vc", Engine.HOST: "host"}[
            self.engine
        ]
        return f"{tag}({x},{y}) {self.op.value:<5} {args}"


@dataclass
class Region:
    """A contiguous span of the device image.

    `word` and `words` are 32-BIT WORDS throughout -- every region, every
    address. `elems` and `note` are carried so a listing says what the span
    holds rather than only where it is.
    """

    name: str
    word: int
    words: int
    elems: int = 0
    note: str = ""

    @property
    def end(self) -> int:
        return self.word + self.words

    def __str__(self) -> str:
        span = f"[{self.word:>8} .. {self.end:>8})"
        size = f"{self.words:>7} words"
        el = f"{self.elems:>8} elems" if self.elems else ""
        return f"{self.name:<4} {span} {size} {el:>14}   {self.note}"


@dataclass
class MemoryMap:
    """Where each operand lives in the device image.

    The packer and the address generator both read this rather than each
    computing its own; a mismatch overlaps two operands and the answer is
    quietly wrong.
    """

    regions: list[Region] = field(default_factory=list)
    #: Second names for regions: `a`, `b`, `c` and `acc` for the roles a single
    #: GEMM's operands play, over regions named after the VALUE they hold. A
    #: graph with several matmuls has no single "the B operand", so the roles
    #: are aliases and the value names are the truth.
    aliases: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, words: int, elems: int = 0, note: str = "") -> Region:
        """Append a region of `words` 32-bit words after the last, and return it."""
        base = self.regions[-1].end if self.regions else 0
        r = Region(name, base, words, elems, note)
        self.regions.append(r)
        return r

    def alias(self, name: str, target: str) -> None:
        """Give `target` the second name `name`."""
        self.aliases[name] = target

    def get(self, name: str) -> Region:
        name = self.aliases.get(name, name)
        for r in self.regions:
            if r.name == name:
                return r
        raise KeyError(f"no region {name!r}; have {[r.name for r in self.regions]}")

    @property
    def total(self) -> int:
        return self.regions[-1].end if self.regions else 0

    def __str__(self) -> str:
        return "\n".join(str(r) for r in self.regions)


@dataclass
class Program:
    """An instruction stream plus the memory map it addresses.

    `rounds` groups instruction indices into host upload/kick/await batches;
    each round is dispatched and awaited before the next.
    """

    insts: list[Inst] = field(default_factory=list)
    memory: MemoryMap = field(default_factory=MemoryMap)
    rounds: list[list[int]] = field(default_factory=list)
    #: How C is laid out: `m`, `n`, tile `tm`/`tn`, `gn` tiles per row, `tiles`
    #: in all, and `rows`/`cols` giving each element's logical position within a
    #: tile. The host needs it to unpack the result and a broadcast operand
    #: needs it to know which column it is on.
    tiling: dict = field(default_factory=dict)
    #: Graph input name -> the region the host uploads it into.
    inputs: dict[str, str] = field(default_factory=dict)
    #: Region name -> how the host must lay that operand out: which GEMM role
    #: it plays, the block shape, and whether a transpose is already folded
    #: into its address so the packer must NOT apply another.
    packing: dict[str, dict] = field(default_factory=dict)

    #: Region name -> `{from, off, run, stride, count}`: a GEMM operand the host
    #: gathers out of a bigger tensor, since a FILL walks one contiguous span.
    windows: dict[str, dict] = field(default_factory=dict)

    #: Region name -> its OWN tile geometry. A graph has as many tilings as
    #: matmul shapes, so `tiling` is only the default for `result()`.
    tilings: dict[str, dict] = field(default_factory=dict)
    #: `(band name, first index, last index + 1)`, in emission order. Dependent
    #: bands cannot overlap, and nothing in the stream marks where one ends.
    bands: list[tuple[str, int, int]] = field(default_factory=list)
    name: str = "program"

    def emit(self, inst: Inst) -> None:
        self.insts.append(inst)

    def nodes(self, engine: Engine | None = None) -> set[tuple[int, int]]:
        """Destination nodes, optionally for one engine only."""
        return {i.node for i in self.insts if engine is None or i.engine is engine}

    def by_engine(self) -> dict[Engine, list[Inst]]:
        out: dict[Engine, list[Inst]] = {}
        for i in self.insts:
            out.setdefault(i.engine, []).append(i)
        return out

    def occupancy(self) -> dict[str, tuple[int, int]]:
        """Instructions and distinct nodes per engine, keyed by engine name."""
        return {
            e.value: (len(v), len({i.node for i in v}))
            for e, v in self.by_engine().items()
        }

    def listing(self, limit: int = 40) -> str:
        """A disassembly, truncated to `limit` instructions."""
        occ = ", ".join(
            f"{name} {n} insts on {k} nodes"
            for name, (n, k) in self.occupancy().items()
        )
        head = (
            f"program {self.name}: {len(self.insts)} instructions, "
            f"{len(self.rounds)} rounds\n  {occ}"
        )
        body = [f"  {i}" for i in self.insts[:limit]]
        if len(self.insts) > limit:
            body.append(f"  ... {len(self.insts) - limit} more")
        return "\n".join([head, *body])

    def __str__(self) -> str:
        return self.listing()
