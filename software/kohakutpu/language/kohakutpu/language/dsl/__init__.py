"""`kt`: KohakuTPU kernels written in Python, traced to L2 text.

import kohakutpu.language.dsl as kt
from kohakutpu.language import machine as MF

@kt.kernel
def silu(x=kt.In("R", 128), y=kt.Out("R", 128), *, rows=16):
    for c in kt.par(2, on=kt.vc):
        for t in kt.range(x.shape[0] // 2 // rows):
            r = c * (x.shape[0] // 2) + t * rows
            v = kt.load(x[r : r + rows, :])
            kt.store(y[r : r + rows, :], v * kt.recip(1.0 + kt.exp2(v * -kt.LOG2E)))

machine = kt.Machine.of(MF.load("multimesh_v9t8"))
print(silu.trace({"x": ((512, 128), "f16")}, machine=machine).text)
"""

from kohakutpu.language.dsl.errors import DSLError, TracerControlFlowError
from kohakutpu.language.dsl.kernel import (
    Auto,
    In,
    Kernel,
    Out,
    Port,
    Traced,
    auto,
    kernel,
)
from kohakutpu.language.dsl.ops import (
    absolute,
    broadcast,
    buffer,
    copy,
    drain,
    exp2,
    exp2d,
    fill,
    fill_value,
    fma,
    fnma,
    gather,
    gemm,
    gemm_tiles,
    gen,
    gt4,
    load,
    log2,
    math,
    maximum,
    minimum,
    pack,
    par,
    quant,
    range,
    recip,
    recv,
    reduce,
    require,
    rmax,
    rsqrt,
    rsum,
    scatter,
    send,
    sticky,
    store,
    unpack,
    when,
    where,
    xlane,
    xrot,
    zero,
)
from kohakutpu.language.dsl.trace import active
from kohakutpu.language.dsl.types import Machine, TensorSpec
from kohakutpu.language.dsl.values import ANY, Param, Unit, Value, View, mg, node, vc

LOG2E = 1.4426950408889634


def machine() -> Machine:
    """The machine the kernel being traced is specialised for."""
    return active().machine


__all__ = [
    "ANY",
    "LOG2E",
    "Auto",
    "DSLError",
    "In",
    "Kernel",
    "Machine",
    "Out",
    "Param",
    "Port",
    "TensorSpec",
    "Traced",
    "TracerControlFlowError",
    "Unit",
    "Value",
    "View",
    "absolute",
    "auto",
    "broadcast",
    "buffer",
    "copy",
    "drain",
    "exp2",
    "exp2d",
    "fill",
    "fill_value",
    "fma",
    "fnma",
    "gather",
    "gemm",
    "gemm_tiles",
    "gen",
    "gt4",
    "kernel",
    "load",
    "log2",
    "machine",
    "math",
    "maximum",
    "mg",
    "minimum",
    "node",
    "pack",
    "par",
    "quant",
    "range",
    "recip",
    "recv",
    "reduce",
    "require",
    "rmax",
    "rsqrt",
    "rsum",
    "scatter",
    "send",
    "sticky",
    "store",
    "unpack",
    "vc",
    "when",
    "where",
    "xlane",
    "xrot",
    "zero",
]
