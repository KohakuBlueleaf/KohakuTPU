"""KohakuTPU's L2 (docs/projects/kohakutpu/ir/l2.md): layouts, the hand-written
schedule builders (`ops`) and the lowerers the framework's L2 -> L1 compiler
runs (`lowerers`)."""

from kohakuaccel.ir.l2.lower import compile as _compile
from kohakutpu.ir.l1.program import Program
from kohakutpu.ir.l2 import layouts, ops
from kohakutpu.ir.l2.lowerers import LOWERERS, mover


def compile(schedule, machine) -> list:
    """L1 programs, one a package, for a KohakuTPU schedule."""
    return _compile(schedule, machine, LOWERERS, mover=mover, program=Program)


__all__ = ["compile", "layouts", "ops"]
