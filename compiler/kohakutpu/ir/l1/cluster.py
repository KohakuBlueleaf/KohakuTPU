"""L1 for a matmul cluster: FILL, GEMM and DRAIN, each one payload word.

Lowered through `kohakutpu.isa.cluster.ISA`. `Gemm.emit` with `Drain.fuse` is the
fused drain, legal only where `ISA.can_emit` says so; this level checks it.
"""

from dataclasses import dataclass

from kohakutpu.isa.cluster import ISA


@dataclass(frozen=True)
class Fill:
    """`n` entries from byte address `addr` into L1 side `sel` (0 A, 1 B)."""

    addr: int
    n: int
    sel: int = 0
    eoff: int = 0
    fbank: int = 0

    def flits(self) -> list[int]:
        return [
            ISA.fill(self.addr, self.n, sel=self.sel, eoff=self.eoff, fbank=self.fbank)
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
                **extra,
            )
        ]


@dataclass(frozen=True)
class Drain:
    """`n` sub-tiles to byte address `addr`, or to node `dst`'s L1 at word `addr // 32`."""

    addr: int
    n: int
    fuse: bool = False
    dst: tuple | None = None
    dflags: int = 0
    ack: tuple = (0, 0)

    def flits(self) -> list[int]:
        node = {}
        if self.dst is not None:
            node = {
                "dnode": 1,
                "dst_x": self.dst[0],
                "dst_y": self.dst[1],
                "dflags": self.dflags,
                "dack_x": self.ack[0],
                "dack_y": self.ack[1],
            }
        return [ISA.drain(self.addr, self.n, fuse=int(self.fuse), **node)]
