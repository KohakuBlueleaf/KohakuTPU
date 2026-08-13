"""What a project tells the framework about its compute units.

The framework knows that every endpoint answers CU_CAPS, CU_STATUS and
CU_COUNTERS identically -- ``noc_cu_base`` keeps those, so their meaning cannot
vary. CU_DBG is the exception: it is the datapath's own pair of counters and
means whatever that unit decided.

``ktpu.hw.device.decode_dbg`` handled this by switching on ``CU_MATMUL`` /
``CU_VECTOR``, which put two of one project's unit types inside the framework's
own address-map module. Here a project registers a decoder instead, and the
framework's fallback for an unregistered type is the same all-``None`` answer
that switch already gave -- the identity occupant of the slot.
"""

from collections.abc import Callable
from dataclasses import dataclass

from kohakuaccel.device.registers import type_code

#: What CU_DBG means when nothing has claimed this unit type. An endpoint that
#: ties the port to zero reports exactly this, so it is a real answer.
UNKNOWN_DBG = {"compute_cycles": None, "memory_cycles": None, "scope": None}


@dataclass(frozen=True)
class UnitType:
    """One kind of compute unit, as the host software understands it.

    `name` is the two ASCII characters the unit reports in CU_CAPS. `decode_dbg`
    turns CU_DBG's 64-bit word into a dict; None means this unit does not
    implement the register. `summary` is shown when enumerating a mesh.
    """

    name: str
    decode_dbg: Callable[[int], dict] | None = None
    summary: str = ""

    @property
    def code(self) -> int:
        """The 16-bit CU_TYPE this unit reports."""
        return type_code(self.name)


class UnitRegistry:
    """The unit types a project has declared.

    Construct one per application rather than relying on the module default when
    two projects must coexist in one process.
    """

    def __init__(self) -> None:
        self._by_code: dict[int, UnitType] = {}

    def register(self, unit: UnitType) -> UnitType:
        """Declare `unit`. Raises :class:`ValueError` on a duplicate type code.

        A silent overwrite would mean the last import wins, and which module
        imported last is not something a caller can reason about.
        """
        if unit.code in self._by_code:
            held = self._by_code[unit.code]
            raise ValueError(
                f"unit type {unit.name!r} is already registered as {held.name!r}"
            )
        self._by_code[unit.code] = unit
        return unit

    def get(self, code: int) -> UnitType | None:
        """The registered unit for `code`, or None if nothing claimed it."""
        return self._by_code.get(code)

    def name_of(self, code: int) -> str:
        """A readable name for `code`, registered or not.

        Falls back to decoding the two ASCII characters, so an unknown endpoint
        still prints what it called itself.
        """
        unit = self._by_code.get(code)
        if unit is not None:
            return unit.name
        return bytes([(code >> 8) & 0xFF, code & 0xFF]).decode("ascii", "replace")

    def decode_dbg(self, word: int, code: int) -> dict:
        """CU_DBG for a unit of type `code`.

        Returns :data:`UNKNOWN_DBG` when the type is unregistered or the unit
        declared no decoder.
        """
        unit = self._by_code.get(code)
        if unit is None or unit.decode_dbg is None:
            return dict(UNKNOWN_DBG)
        return unit.decode_dbg(word)

    def __len__(self) -> int:
        return len(self._by_code)

    def __iter__(self):
        return iter(self._by_code.values())


#: The registry a project populates at import time when one is enough.
UNITS = UnitRegistry()


def register(unit: UnitType) -> UnitType:
    """Declare `unit` in the default registry."""
    return UNITS.register(unit)
