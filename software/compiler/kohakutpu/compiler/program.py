"""KohakuTPU's L1 program: the framework's (`kohakuaccel.compiler.program`)
with a vector core's stream lowered against what the core already holds.

`resident` maps a vector core's coordinate to its `imem.Resident`, kept by the
caller across packages: an image the core holds and a descriptor field already
at its value are not sent again (MEASURED: every word is a completion the node
retires, and a burst of image words stalled the other core's fills 23k cycles).
"""

from typing import ClassVar

from kohakuaccel.compiler.program import Program as _Program
from kohakutpu.compiler import imem


def _vector(words: list, resident: dict, coord) -> list:
    if coord not in resident:
        resident[coord] = imem.Resident(getattr(resident, "words", imem.IMEM_WORDS))
    return imem.rewrite(words, resident[coord])


class Program(_Program):
    lowerings: ClassVar[dict] = {"VC": _vector}


__all__ = ["Program"]
