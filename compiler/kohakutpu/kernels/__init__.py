"""The kernel library: PRE-BUILT kernels that fuse work worth fusing.

Everything here does more than one thing in one pass -- a matmul with its
epilogue on the resident tile, a norm with no intermediate in memory, attention
that never materialises the score matrix.

ONE IMPLEMENTATION PER KERNEL: two means a caller can pick the slow one. The
staged and separated variants these were graded against are gone; their
measurements are recorded with each kernel.

The single-operator kernels the TENSOR level is made of are `kohakutpu.ops` --
one pass, one operator, nothing composed. A plain `matmul` and every elementwise
op live THERE and are not re-exported here: `ops.matmul` takes `...`, so it
covers the rank-2 case too, and two names for one kernel is how a caller picks
the slow one.
"""

from kohakutpu.kernels.activation import (
    LOG2E,
    gelu_tanh,
    linear_add,
    linear_add_gelu,
    linear_add_relu,
    linear_add_silu,
    linear_bias,
    linear_gate_add,
    linear_gelu,
    linear_relu,
    linear_scale,
    linear_silu,
    sigmoid,
)
from kohakutpu.kernels.attention import NEG, flash_attention
from kohakutpu.kernels.conv2d import conv2d_bias
from kohakutpu.kernels.fused import group_norm_fused as group_norm
from kohakutpu.kernels.fused import layernorm_fused as layernorm
from kohakutpu.kernels.fused import rmsnorm_fused as rmsnorm
from kohakutpu.kernels.fused import softmax_fused as softmax
from kohakutpu.kernels.groupnorm import group_norm_silu, group_stats
from kohakutpu.kernels.mlp import geglu, mlp, swiglu
from kohakutpu.kernels.wide import (
    layernorm_wide,
    rmsnorm_wide,
    softmax_wide,
    split,
)

__all__ = [
    "LOG2E",
    "NEG",
    "conv2d_bias",
    "flash_attention",
    "geglu",
    "gelu_tanh",
    "group_norm",
    "group_norm_silu",
    "group_stats",
    "layernorm",
    "layernorm_wide",
    "linear_add",
    "linear_add_gelu",
    "linear_add_relu",
    "linear_add_silu",
    "linear_bias",
    "linear_gate_add",
    "linear_gelu",
    "linear_relu",
    "linear_scale",
    "linear_silu",
    "mlp",
    "rmsnorm",
    "rmsnorm_wide",
    "sigmoid",
    "softmax",
    "softmax_wide",
    "split",
    "swiglu",
]
