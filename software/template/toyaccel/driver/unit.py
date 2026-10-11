"""The saxpy compute unit's type registration: what lets :mod:`kohakuaccel`
name this endpoint and decode its CU_DBG without knowing anything about saxpy.
The datapath's stand-in is `toyaccel.simulation.unit`.
"""

from kohakuaccel.driver.device import type_code
from kohakuaccel.driver.unit import UnitType, register

#: Two ASCII characters, so an unknown endpoint still prints something readable.
SAXPY = "SX"
SAXPY_CODE = type_code(SAXPY)


def decode_dbg(word: int) -> dict:
    """CU_DBG for a saxpy unit: elements processed, in the low half.

    The framework knows CU_DBG exists and that it is 64 bits; what those bits
    mean is this unit's business, which is why the decoder is registered rather
    than built in.
    """
    return {
        "compute_cycles": None,
        "memory_cycles": None,
        "elements": word & 0xFFFF_FFFF,
        "scope": "cumulative",
    }


UNIT = register(
    UnitType(
        name=SAXPY,
        decode_dbg=decode_dbg,
        summary="y = a*x + y over float32, one instruction",
    )
)
