"""One kernel per TENSOR-LEVEL operator, and nothing composed.

Level 5's backing, and the answer to "what must a tinygrad backend implement".
A pre-built FUSED kernel is a different topic and lives in `kernels/`: one op
here is a whole pass over the operand, so a caller stringing several together
is paying per pass and should reach for a kernel instead.

EVERY KERNEL STATES ITS TILING. `part` is elements per instance, so a caller
who wants a different split passes one; a bare `y <<= f(x)` says nothing about
how the work divides and leaves the extent to a grid the compiler invented.
That is the "near full control" end -- `api.py` above this is "near none".
"""

from kohakutpu.ops.activation import gelu, relu, sigmoid, silu
from kohakutpu.ops.conv2d import (
    PLANE,
    PLANE2,
    conv2d,
    conv2d_stride2,
    conv2d_upsample2,
    positions,
    weights_for_k,
    weights_for_upsample2,
)
from kohakutpu.ops.elementwise import (
    absolute,
    div,
    exp2,
    log2,
    maximum,
    minimum,
    mul,
    neg,
    recip,
    residual,
    rsqrt,
    sub,
    where,
)
from kohakutpu.ops.matmul import matmul
from kohakutpu.ops.norm import layernorm, rmsnorm, softmax, softmax_keys
from kohakutpu.ops.reduce import row_max, row_sum

#: log2(e). Every exponential here is `exp2`, which is the op the core has.
LOG2E = 1.4426950408889634

__all__ = [
    "LOG2E",
    "PLANE",
    "PLANE2",
    "absolute",
    "conv2d",
    "conv2d_stride2",
    "conv2d_upsample2",
    "div",
    "exp2",
    "gelu",
    "layernorm",
    "log2",
    "matmul",
    "maximum",
    "minimum",
    "mul",
    "neg",
    "positions",
    "recip",
    "relu",
    "residual",
    "rmsnorm",
    "row_max",
    "row_sum",
    "rsqrt",
    "sigmoid",
    "silu",
    "softmax",
    "softmax_keys",
    "sub",
    "weights_for_k",
    "weights_for_upsample2",
    "where",
]
