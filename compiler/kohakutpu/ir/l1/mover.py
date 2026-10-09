"""L1 for the node's mover: one op a move, lowered to its register writes by
`kohakuaccel.package.mover`."""

from dataclasses import dataclass

from kohakuaccel.package import mover as PM


@dataclass(frozen=True)
class Quantise:
    """`entries` FP16 entries at `src` (eight words each) to MXFP7 at `dst`."""

    src: int
    dst: int
    entries: int

    def writes(self) -> list:
        return PM.convert(self.src, self.dst, self.entries)


@dataclass(frozen=True)
class Copy:
    """`nbytes` (whole words) from `src` to `dst`."""

    src: int
    dst: int
    nbytes: int

    def writes(self) -> list:
        return PM.copy(self.src, self.dst, self.nbytes)
