"""A matmul cluster's instructions: FILL, GEMM and DRAIN, each one payload
word, encoded through `isa.cluster.ISA`. `Gemm.emit` with `Drain.fuse` is the
fused drain, legal only where `ISA.can_emit` says so; this checks it.

An op's `patches` (`kohakuaccel.compiler.program.Patch`, word 0) set fields
from the node's registers when it reaches the word; :func:`segments` names a
field's bits.
"""

from dataclasses import dataclass

from kohakutpu.compiler.isa.cluster import ISA

#: DRAIN to a unit: which of a cluster's buffers (isa.md s5.3).
BUF_L1A, BUF_L1B, BUF_ACC = 0, 1, 2


def segments(name: str) -> tuple:
    """``((bit, width, shift), ...)`` of field `name` in a cluster word: an
    address is two (its low 34 bits and its high 6)."""
    fmt = ISA.FILL
    if name == "addr":
        lo, wlo = fmt.span("addr")
        hi, whi = fmt.span("addr_hi")
        return ((lo, wlo, 0), (hi, whi, wlo))
    bit, width = fmt.span(name)
    return ((bit, width, 0),)


def coord_byte(xy) -> int:
    """A coordinate as the ISA's {y, x} byte."""
    return (xy[1] & 0xF) << 4 | (xy[0] & 0xF)


@dataclass(frozen=True)
class Fill:
    """`n` entries from byte address `addr` into L1 side `sel` (0 A, 1 B).
    `peers` are up to three sharers' coordinates: the lowest {y, x} of the set
    reads once for all; a follower must already be in this FILL when the
    entries come, which the node's mark / wait orders."""

    addr: int
    n: int
    sel: int = 0
    eoff: int = 0
    fbank: int = 0
    peers: tuple = ()
    patches: tuple = ()

    def flits(self) -> list[int]:
        shared = ISA.peers([coord_byte(p) for p in self.peers]) if self.peers else {}
        return [
            ISA.fill(
                self.addr,
                self.n,
                sel=self.sel,
                eoff=self.eoff,
                fbank=self.fbank,
                **shared,
            )
        ]


@dataclass(frozen=True)
class Gemm:
    gm: int
    gn: int
    nk: int
    acc: bool = False
    aoff: int = 0
    boff: int = 0
    abank: int = 0
    bbank: int = 0
    #: With `emit`, `addr` is where the sub-tiles stream as they complete.
    emit: bool = False
    addr: int = 0
    anchor: int = ISA.cfg.anchor
    patches: tuple = ()

    def __post_init__(self) -> None:
        # No word carries `addr` without `emit`: one GEMM, one value.
        if not self.emit:
            object.__setattr__(self, "addr", 0)

    def flits(self) -> list[int]:
        if not ISA.legal_nk(self.nk):
            raise ValueError(
                f"a GEMM of nk {self.nk}: the pumped sweep takes K-blocks in pairs, "
                f"and an odd nk >= 3 corrupts sub-tile (0, 0); use 1 or an even nk"
            )
        if self.emit and not ISA.can_emit(int(self.acc), self.nk):
            raise ValueError(
                f"an emitting GEMM (nk {self.nk}, acc {int(self.acc)}) also opens "
                f"its tile; the fused DRAIN would wait forever"
            )
        extra = {"emit": 1, **ISA.split_addr(self.addr)} if self.emit else {}
        return [
            ISA.gemm(
                self.gm,
                self.gn,
                self.nk,
                acc=int(self.acc),
                aoff=self.aoff,
                boff=self.boff,
                abank=self.abank,
                bbank=self.bbank,
                anchor=self.anchor,
                **extra,
            )
        ]


@dataclass(frozen=True)
class Drain:
    """`n` sub-tiles to byte address `addr`, or to node `dst`'s buffer `buf`
    at granule `addr // 32`. `mesh` / `fin` aim it into another mesh: `fin`
    is the final coordinate there (a unit, or that mesh's memory port for a
    memory drain), and `dst` then names this mesh's memory port."""

    addr: int
    n: int
    fuse: bool = False
    dst: tuple | None = None
    dflags: int = 0
    ack: tuple = (0, 0)
    buf: int = BUF_L1A
    mesh: int = 0
    fin: tuple | None = None
    patches: tuple = ()

    def flits(self) -> list[int]:
        node = {}
        if self.dst is not None:
            node = {
                "dnode": 1,
                "dst_x": self.dst[0],
                "dst_y": self.dst[1],
                "dbuf": self.buf,
                "dflags": self.dflags,
                "dack_x": self.ack[0],
                "dack_y": self.ack[1],
            }
        if self.fin is not None:
            node |= {"dmesh": self.mesh, "dfin": coord_byte(self.fin)}
        return [ISA.drain(self.addr, self.n, fuse=int(self.fuse), **node)]
