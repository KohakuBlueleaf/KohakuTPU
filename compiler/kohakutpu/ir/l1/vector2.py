"""L1 for a V2 vector core: what the node sends it.

A V2 kernel is built at L0 by `kohakutpu.hw.vector2.Kernel2` (instruction words,
descriptor fields, a start pc). `Kernel` sends one whole -- descriptors, image,
RUN -- and the program's residency rewrite (`kohakutpu.imem`, with
``Cores(IMEM_WORDS_V2)`` as the state) drops what the core already holds, so a
caller sends the whole kernel every time. `Desc`, `Dims` and `Run` are v1's:
those flits are the same on both cores.
"""

from dataclasses import dataclass

from kohakutpu.hw import vector2 as V2
from kohakutpu.ir.l1.vector import Desc, Dims, Run


@dataclass(frozen=True)
class Kernel:
    """A `Kernel2`'s descriptors, image and RUN."""

    kern: object

    def flits(self) -> list[int]:
        return self.kern.flits()


@dataclass(frozen=True)
class Image:
    """Instruction memory from word `at`: raw V2 words."""

    words: tuple
    at: int = 0

    def flits(self) -> list[int]:
        if self.at + len(self.words) > V2.IMEM_WORDS:
            raise ValueError(
                f"an image of {len(self.words)} words at {self.at} passes "
                f"IMEM's {V2.IMEM_WORDS}"
            )
        return [V2.imem_flit(self.at + i, w) for i, w in enumerate(self.words)]


__all__ = ["Desc", "Dims", "Image", "Kernel", "Run"]
