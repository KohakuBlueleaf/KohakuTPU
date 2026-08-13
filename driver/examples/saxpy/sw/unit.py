"""The saxpy compute unit: its type registration and its simulation model.

Two things a project must supply, and nothing else. The type registration is
what lets :mod:`kohakuaccel` name this endpoint and decode its CU_DBG without
knowing anything about saxpy; the model is what stands in for the datapath until
the RTL exists.
"""

import struct

from kohakuaccel.device import SIG_INST_COMPLETE, encode_caps, type_code
from kohakuaccel.sim import Memory, Signal, UnitModel
from kohakuaccel.unit import UnitType, register

#: Two ASCII characters, so an unknown endpoint still prints something readable.
SAXPY = "SX"
SAXPY_CODE = type_code(SAXPY)

ELEM = 4  # float32


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


class SaxpyUnit(UnitModel):
    """A simulated saxpy datapath.

    Counts the elements it has processed so a CU_DBG read has something true to
    report.
    """

    def __init__(self, version: int = 1) -> None:
        self.caps = encode_caps(SAXPY_CODE, version=version, buffers=1)
        self.elements = 0

    def execute(self, flit: int, mem: Memory) -> list[Signal]:
        """Run one instruction and report it retired.

        Reads `n` float32 values from each of `x_addr` and `y_addr`, writes the
        result back over `y`, and returns a single SIG_INST_COMPLETE -- which is
        what the dispatcher's credit accounting and the host's wait are both
        counting.
        """
        from . import isa

        op = isa.decode(flit & ((1 << 256) - 1))
        n, a = op["n"], op["a"]
        if n == 0:
            return [Signal(SIG_INST_COMPLETE)]

        x = struct.unpack(f"<{n}f", mem.read(op["x_addr"], n * ELEM))
        y = struct.unpack(f"<{n}f", mem.read(op["y_addr"], n * ELEM))
        out = [a * xi + yi for xi, yi in zip(x, y)]
        mem.write(op["y_addr"], struct.pack(f"<{n}f", *out))

        self.elements += n
        return [Signal(SIG_INST_COMPLETE)]
