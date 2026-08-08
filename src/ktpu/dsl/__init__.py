"""The tracing frontend. See docs/driver/dsl.md.

    from ktpu.dsl import kernel, rsqrt

    @kernel
    def rmsnorm(x, g, eps: float):
        ms = (x * x).mean(axis=-1, keepdim=True)
        return x * rsqrt(ms + eps) * g

    graph = rmsnorm(TensorSpec((256, 1024), FP16), TensorSpec((1024,), FP16), 1e-6)

Nothing above is parsed. `x * x` calls `Tracer.__mul__`, which appends a `mul`
op and returns a new tracer; the returned tracers name the outputs and the
recorded ops are the graph. Which is why a helper function inlines, a
comprehension unrolls and a constant folds -- all of it is just Python running.

The one rule, and the reason this is sound rather than a trap: **compile-time
control flow is free, runtime control flow is an error.** Branching on a plain
int unrolls; branching on a tracer raises `TracerControlFlowError` naming the
line. Data-dependent selection is still available, as a value: `where(c, a, b)`.

This layer emits level-1 graph IR and knows nothing below it -- no tiles, no
grid, no engines.
"""

from ktpu.dsl.context import TraceContext
from ktpu.dsl.entry import (
    NUMPY_DTYPES,
    Kernel,
    TensorSpec,
    kernel,
    spec_of,
    trace,
)
from ktpu.dsl.errors import (
    DSLError,
    TracerControlFlowError,
    TracerDTypeError,
    TracerShapeError,
)
from ktpu.dsl.library import (
    gelu,
    layernorm,
    rmsnorm,
    sigmoid,
    silu,
    softmax,
    split_k_epilogue,
    tanh,
)
from ktpu.dsl.ops import (
    LN_2,
    LOG2_E,
    cast,
    dot,
    exp,
    exp2,
    fma,
    log,
    log2,
    maximum,
    minimum,
    neg,
    recip,
    relu,
    rsqrt,
    sqrt,
    where,
)
from ktpu.dsl.tracer import Tracer

__all__ = [
    "LN_2",
    "LOG2_E",
    "NUMPY_DTYPES",
    "DSLError",
    "Kernel",
    "TensorSpec",
    "TraceContext",
    "Tracer",
    "TracerControlFlowError",
    "TracerDTypeError",
    "TracerShapeError",
    "cast",
    "dot",
    "exp",
    "exp2",
    "fma",
    "gelu",
    "kernel",
    "layernorm",
    "log",
    "log2",
    "maximum",
    "minimum",
    "neg",
    "recip",
    "relu",
    "rmsnorm",
    "rsqrt",
    "sigmoid",
    "silu",
    "softmax",
    "spec_of",
    "split_k_epilogue",
    "sqrt",
    "tanh",
    "trace",
    "where",
]
