"""The three-level IR, and the infrastructure a project inherits with it.

Every workload in scope has all three levels; only the content differs.

    L3 graph      what the work is           project vocabulary, framework shape
    L2 schedule   where and when it happens  entirely the framework's
    L1 program    encoded instructions       project words, framework layout
"""

from kohakuaccel.ir.base import DepGraph, IRError, Level, Node
from kohakuaccel.ir.l1 import ProgramIR, UnitProgram
from kohakuaccel.ir.l2 import Coord, Policy, Region, Round, ScheduleIR, Task
from kohakuaccel.ir.l3 import Buffer, Domain, GraphIR, Op

__all__ = [
    "Buffer",
    "Coord",
    "DepGraph",
    "Domain",
    "GraphIR",
    "IRError",
    "Level",
    "Node",
    "Op",
    "Policy",
    "ProgramIR",
    "Region",
    "Round",
    "ScheduleIR",
    "Task",
    "UnitProgram",
]
