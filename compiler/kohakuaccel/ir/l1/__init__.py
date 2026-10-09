"""L1, the program: per-unit instruction streams, in two forms.

`kohakuaccel.ir.l1.program.Program` is what a kernel is written in: typed
per-unit streams and the node's sync points, lowered to an L0 package and
nothing else. `ProgramIR` (`staged`) is the pass pipeline's form: encoded flits
laid into staging slots per round.

`program` is imported by its own path: it lowers through `kohakuaccel.package`,
which the backend layer imports, which imports this package.
"""

from kohakuaccel.ir.l1.staged import ProgramIR, UnitProgram

__all__ = ["ProgramIR", "UnitProgram"]
