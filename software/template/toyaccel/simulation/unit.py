"""The saxpy unit's simulation model: what stands in for the datapath until the
RTL exists."""

import struct

from kohakuaccel.driver.device import SIG_INST_COMPLETE, encode_caps
from kohakuaccel.simulation.machine import Memory, Signal, UnitModel
from toyaccel.driver import isa
from toyaccel.driver.unit import SAXPY_CODE

ELEM = 4  # float32


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
        op = isa.decode(flit & ((1 << 256) - 1))
        n, a = op["n"], op["a"]
        if n == 0:
            return [Signal(SIG_INST_COMPLETE)]

        x = struct.unpack(f"<{n}f", mem.read(op["x_addr"], n * ELEM))
        y = struct.unpack(f"<{n}f", mem.read(op["y_addr"], n * ELEM))
        out = [a * xi + yi for xi, yi in zip(x, y, strict=True)]
        mem.write(op["y_addr"], struct.pack(f"<{n}f", *out))

        self.elements += n
        return [Signal(SIG_INST_COMPLETE)]
