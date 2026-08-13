"""The kernel DSL framework: structure here, vocabulary in the project.

A kernel is traced ONCE with symbolic extents and CALLED per shape. The
framework provides the grid, the loop, the symbolic arithmetic, the signature
and the compile step; what a port and a statement MEAN -- `fill`, `gemm`,
`trace`, `shade`, `tap` -- is declared by whatever project is using it.
"""

from kohakuaccel.lang.dims import Dim, DimError, Name, ceildiv, dims, free, resolve
from kohakuaccel.lang.iface import (
    IfaceError,
    In,
    Out,
    Port,
    PortSpec,
    Signature,
    bind,
    is_port,
    read,
    solve,
)
from kohakuaccel.lang.kernel import (
    Backend,
    Compiled,
    Instance,
    Kernel,
    KernelError,
    kernel,
)
from kohakuaccel.lang.record import (
    Affine,
    Index,
    Loop,
    RecordError,
    Stmt,
    Trace,
    active,
    grid,
    loop,
    stage,
    sweep,
    units,
)

__all__ = [
    "Affine",
    "Backend",
    "Compiled",
    "Dim",
    "DimError",
    "IfaceError",
    "In",
    "Index",
    "Instance",
    "Kernel",
    "KernelError",
    "Loop",
    "Name",
    "Out",
    "Port",
    "PortSpec",
    "RecordError",
    "Signature",
    "Stmt",
    "Trace",
    "active",
    "bind",
    "ceildiv",
    "dims",
    "free",
    "grid",
    "is_port",
    "kernel",
    "loop",
    "read",
    "resolve",
    "solve",
    "stage",
    "sweep",
    "units",
]
