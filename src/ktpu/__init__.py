"""KohakuTPU compiler and runtime.

Three IR levels -- graph, schedule, target -- plus the runtime that uploads and
kicks. See docs/driver/README.md for the stack and docs/driver/ir.md for the
levels.

`driver/` is the previous driver. It still runs a GEMM end to end and is kept as
reference; nothing here has replaced it yet.
"""

from ktpu.dsl import TensorSpec, kernel, trace

__version__ = "0.1.0"

__all__ = ["TensorSpec", "kernel", "trace"]
