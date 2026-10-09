"""L1: the instruction stream per unit, typed, and the program that sequences them."""

from kohakutpu.ir.l1 import cluster, vector
from kohakutpu.ir.l1.program import Program

__all__ = ["Program", "cluster", "vector"]
