"""The saxpy machine's description.

Everything KohakuAccel needs to know about this accelerator that it cannot read
off the hardware. Compare ``ktpu.target.Target``, which carried the framework's
dispatch limits and eleven KohakuTPU capacities in one class.
"""

from dataclasses import dataclass
from typing import ClassVar

from kohakuaccel.machine import Target

#: Where the example places its vectors. Real machines take this from a board
#: description; two constants are enough here.
X_ADDR = 0x0001_0000
Y_ADDR = 0x0002_0000


@dataclass(frozen=True)
class SaxpyMachine(Target):
    """A mesh of saxpy units.

    `units` is where they sit, in dispatch order. `max_elements` is the longest
    vector one instruction may name -- a real bound, since the unit reads the
    whole span before writing any of it.
    """

    #: This family's optional hardware. A unit without it computes the same
    #: answer in two passes, so codegen may only EMIT for it, never assume it.
    FEATURES: ClassVar[tuple[str, ...]] = ("fused_multiply_add",)

    name: str = "saxpy-2"
    units: tuple[tuple[int, int], ...] = ((1, 0), (2, 0))
    max_elements: int = 4096

    def split(self, n: int) -> list[tuple[tuple[int, int], int, int]]:
        """Divide `n` elements across the units as ``(coord, offset, count)``.

        The remainder goes to the earlier units, one element each, so the widest
        share is never more than one element above the narrowest. Units that
        would receive nothing are omitted rather than dispatched an empty
        instruction, which would still cost a round trip.
        """
        if n > self.max_elements * len(self.units):
            raise ValueError(
                f"{n} elements exceeds {len(self.units)} units x {self.max_elements}"
            )
        base, extra = divmod(n, len(self.units))
        out, off = [], 0
        for i, coord in enumerate(self.units):
            count = base + (1 if i < extra else 0)
            if count:
                out.append((coord, off, count))
                off += count
        return out
