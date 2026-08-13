"""KohakuTPU's kernel vocabulary: what a statement MEANS on this machine.

Level 4. Two kinds of unit and one decorator: a kernel is a sequence of stages,
and each stage runs on whichever unit its statements need.

    cluster.py   FILLED and SWEPT   `ra <<= a[i, k]`, `acc += ra @ rb`, `<<=`
    vector.py    PROGRAMMED         one expression over lanes
    buffers.py   what is read and written, and the temps between stages
    backend.py   what all of it means in memory and in bytes
"""

from kohakutpu.hw.vector import VLMAX
from kohakutpu.lang.backend import BACKEND, TpuBackend, kernel
from kohakutpu.lang.buffers import (
    KBLOCK,
    LANES,
    Buffer,
    In,
    Out,
    Plane,
    Spread,
    Tap,
    plane,
    table,
    temp,
)
from kohakutpu.lang.cluster import Product, Region, Slice, Tile, region, tile
from kohakutpu.lang.errors import LangError
from kohakutpu.lang.vector import (
    Const,
    Node,
    Operand,
    Part,
    Reduction,
    Ref,
    Resident,
    Value,
    absolute,
    equal,
    exp2,
    fma,
    greater,
    less,
    log2,
    maximum,
    minimum,
    recip,
    row_max,
    row_sum,
    rsqrt,
    sqrt_approx,
    sqrt_newton,
    where,
)

__all__ = [
    "BACKEND",
    "KBLOCK",
    "LANES",
    "VLMAX",
    "Buffer",
    "Const",
    "In",
    "LangError",
    "Node",
    "Operand",
    "Out",
    "Part",
    "Plane",
    "Product",
    "Reduction",
    "Ref",
    "Region",
    "Resident",
    "Slice",
    "Spread",
    "Tap",
    "Tile",
    "TpuBackend",
    "Value",
    "absolute",
    "equal",
    "exp2",
    "fma",
    "greater",
    "kernel",
    "less",
    "log2",
    "maximum",
    "minimum",
    "plane",
    "recip",
    "region",
    "row_max",
    "row_sum",
    "rsqrt",
    "sqrt_approx",
    "sqrt_newton",
    "table",
    "temp",
    "tile",
    "where",
]
