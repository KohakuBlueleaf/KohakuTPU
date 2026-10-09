"""KohakuTPU's L2 (docs/projects/kohakutpu/ir/l2.md): layouts, the hand-written
schedule builders (`ops`), the lowerers the framework's L2 -> L1 compiler runs
(`lowerers`) and the text (`text`)."""

from kohakuaccel.ir.l2.lower import compile as _compile
from kohakutpu.ir.l1 import text as l1_text
from kohakutpu.ir.l1.program import Program
from kohakutpu.ir.l2 import layouts, ops, text
from kohakutpu.ir.l2.lowerers import LOWERERS, mover


def compile(schedule, machine=None) -> list:
    """L1 programs, one a package, for a KohakuTPU schedule, on `machine` or
    the schedule's."""
    machine = machine if machine is not None else schedule.machine
    return _compile(schedule, machine, LOWERERS, mover=mover, program=Program)


def lower(l2: str, machine=None, file: str = "<l2>") -> str:
    """The L2 -> L1 compiler between texts: L2 text in, L1 text out."""
    schedule = text.read(l2, machine, file)
    return l1_text.write(compile(schedule), schedule.machine)


__all__ = ["compile", "layouts", "lower", "ops", "text"]
