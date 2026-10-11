"""L1 for a vector core: its instructions, and what the node sends it.

An `Image` is instruction-memory contents; `Desc`/`Dims` set an address
descriptor; `Run` starts the image. Every type lowers to words through
`kohakutpu.hw.vector` and decides nothing.
"""

from dataclasses import dataclass

from kohakutpu.hw import vector as V


# ------------------------------------------------------------- instructions
@dataclass(frozen=True)
class Alu:
    """`op vd, va, vb, vc` with each source's selector (V, S, C or K)."""

    op: str
    vd: int
    va: int = 0
    vb: int = 0
    vc: int = 0
    sa: int = V.SRC_V
    sb: int = V.SRC_V
    sc: int = V.SRC_V
    pr: int = 0
    pm: int = 0

    def words(self) -> list[int]:
        return [
            V.alu(
                self.op,
                self.vd,
                self.va,
                self.vb,
                self.vc,
                self.sa,
                self.sb,
                self.sc,
                self.pr,
                self.pm,
            )
        ]


@dataclass(frozen=True)
class Chain:
    """`ops` as ONE D2/D4 group (`vec_core.v` S_GA..S_GD): the first reads
    vectors, each later one reads the previous stage's result (source C), S or
    K -- never a vector register -- and only the last writes, its `vd`. Runs in
    the mode of its length, at ``len(ops)`` beats a chunk."""

    ops: tuple

    def __post_init__(self) -> None:
        if len(self.ops) not in (2, 4):
            raise ValueError(f"a chain of {len(self.ops)}; D2 and D4 take 2 or 4")
        if V.SRC_C in (self.ops[0].sa, self.ops[0].sb, self.ops[0].sc):
            raise ValueError("a chain's first stage reads no previous stage")
        for op in self.ops[1:]:
            if V.SRC_V in (op.sa, op.sb, op.sc):
                raise ValueError(f"chain stage {op.op} names a vector register")

    @property
    def mode(self) -> int:
        return V.D2 if len(self.ops) == 2 else V.D4

    def words(self) -> list[int]:
        return [w for op in self.ops for w in op.words()]


def stage(
    op: str, b: int = 0, c: int = 0, sb: int = V.SRC_C, sc: int = V.SRC_C, vd: int = 0
) -> Alu:
    """A chain stage reading the previous result at `va`; `b`/`c` and their
    selectors (S or K) for a second and third operand, C when unused; `vd` is
    read only on the last stage."""
    return Alu(op, vd=vd, va=0, vb=b, vc=c, sa=V.SRC_C, sb=sb, sc=sc)


@dataclass(frozen=True)
class Vred:
    """`S[sd] = reduce(va)` in TREE mode; EXPSUM also writes ``exp2(va)`` to
    `vb`. VL a multiple of 16. The core waits for the reduction to land."""

    sd: int
    va: int
    kind: str = "SUM"
    vb: int = 0

    def words(self) -> list[int]:
        return [V.vred(self.sd, self.va, self.kind, self.vb)]


@dataclass(frozen=True)
class Vld:
    vd: int
    ad: int
    off: int = 0
    dt: int = V.DT_FP16

    def words(self) -> list[int]:
        return [V.vld(self.vd, self.ad, self.off, self.dt)]


@dataclass(frozen=True)
class Vst:
    vs: int
    ad: int
    off: int = 0
    dt: int = V.DT_FP16

    def words(self) -> list[int]:
        return [V.vst(self.vs, self.ad, self.off, self.dt)]


@dataclass(frozen=True)
class Vshuf:
    """``vd[l] = va[(l + S[srot]) % 16]`` within each chunk, predicated by `pr`/`pm`."""

    vd: int
    va: int
    srot: int
    pr: int = 0
    pm: int = 0

    def words(self) -> list[int]:
        return [
            V.alu("VSHUF", vd=self.vd, va=self.va, vb=self.srot, pr=self.pr, pm=self.pm)
        ]


@dataclass(frozen=True)
class Vfill:
    ad: int
    l1: int = 0

    def words(self) -> list[int]:
        return [V.vfill(self.ad, self.l1)]


@dataclass(frozen=True)
class Vdrain:
    ad: int
    l1: int = 0
    node: tuple | None = None
    buf: int = 0
    signal: bool = False

    def words(self) -> list[int]:
        return [V.vdrain(self.ad, self.l1, self.node, self.buf, self.signal)]


@dataclass(frozen=True)
class Seti:
    """`S[sreg] = imm` (raw 24 bits); `to_k` writes K3."""

    sreg: int
    imm: int
    to_k: bool = False

    def words(self) -> list[int]:
        return [V.vseti(self.sreg, self.to_k), self.imm & 0xFFFFFF]


@dataclass(frozen=True)
class Setvl:
    sreg: int

    def words(self) -> list[int]:
        return [V.vsetvl(self.sreg)]


@dataclass(frozen=True)
class Setmode:
    mode: int

    def words(self) -> list[int]:
        return [V.vsetmode(self.mode)]


@dataclass(frozen=True)
class Loop:
    """Repeat the next `body` words `S[sreg]` times."""

    sreg: int
    body: int

    def words(self) -> list[int]:
        return [V.vloop(self.sreg, self.body)]


@dataclass(frozen=True)
class Bar:
    def words(self) -> list[int]:
        return [V.vbar()]


@dataclass(frozen=True)
class Halt:
    def words(self) -> list[int]:
        return [V.vhalt()]


def assemble(code) -> list[int]:
    """Instruction-memory words for a sequence of instructions."""
    return [w for inst in code for w in inst.words()]


# ------------------------------------------------------- what the node sends
@dataclass(frozen=True)
class Image:
    """Instruction memory from word `at`."""

    code: tuple
    at: int = 0

    def flits(self) -> list[int]:
        words = assemble(self.code)
        if self.at + len(words) > 512:
            raise ValueError(
                f"an image of {len(words)} words at {self.at} passes IMEM's 512"
            )
        return [V.imem_flit(self.at + i, w) for i, w in enumerate(words)]


@dataclass(frozen=True)
class Desc:
    """Descriptor `ad`'s base (a byte address in memory, a word in L1)."""

    ad: int
    base: int

    def flits(self) -> list[int]:
        return [V.desc_flit(self.ad, 0, self.base)]


@dataclass(frozen=True)
class Dims:
    """Descriptor `ad`'s walk, ``(stride, bound)`` innermost first; unset dims reset."""

    ad: int
    dims: tuple

    def flits(self) -> list[int]:
        full = list(self.dims) + [(0, 1)] * (4 - len(self.dims))
        if len(full) > 4:
            raise ValueError(f"{len(self.dims)} dims; the AGU walks four")
        return [
            V.desc_flit(self.ad, n + 1, V.dim(s, b)) for n, (s, b) in enumerate(full)
        ]


@dataclass(frozen=True)
class Run:
    pc: int = 0

    def flits(self) -> list[int]:
        return [V.run_flit(self.pc)]
