"""KohakuTPU's L3 ops, each one hardware feature (the table and the unit each
maps to: docs/projects/kohakutpu/ir/l3.md §1), and `mx7`, the clusters'
operand format: a value quantised the way ``mx_quant.v`` quantises it."""

import numpy as np
from kohakuaccel.ir.l3.ops import OpSet, ShapeError, broadcast, fmt_shape, reduced
from kohakutpu.hw.mxfp7 import KBLOCK, value_fp16

OPS = OpSet("kohakutpu")


def _mx7(x):
    x = np.asarray(x, np.float64)
    if x.ndim == 0 or x.shape[-1] % KBLOCK:
        raise ValueError(f"mx7 blocks the last axis in {KBLOCK}s; got {x.shape}")
    return value_fp16(x)


OPS.dtype("mx7", _mx7)


def _blocked(d, what: str) -> None:
    if isinstance(d, int) and d % KBLOCK:
        raise ShapeError(f"{what} {d} is not whole {KBLOCK}-blocks of MXFP7")


# ---------------------------------------------------------------- cluster
def _mmt_shape(shapes, attrs):
    a, b = shapes
    if len(a) != 2 or len(b) != 2 or a[1] != b[1]:
        raise ShapeError(f"{fmt_shape(a)} @ {fmt_shape(b)}^T")
    _blocked(a[1], "K")
    return (a[0], b[0])


# The clusters read MXFP7 only, quantised once (host packing or the mover):
# MXFP7 quantised again is not itself (MEASURED, value_fp16 not idempotent).
MX7 = ("mx7",)
#: What the vector cores load and store (VLD/VST dtypes), and literals.
VEC = ("f16", "f32", "bool", "i32", "const")

OPS.op("mmt", 2, _mmt_shape, lambda xs, at, sh: xs[0] @ xs[1].T, operands=MX7)


def _conv_shape(shapes, attrs):
    x, w = shapes
    if len(x) != 3 or len(w) != 4 or w[3] != x[2] or tuple(w[1:3]) != (3, 3):
        raise ShapeError(
            f"conv3x3 of {fmt_shape(x)} by {fmt_shape(w)}, taps [O, 3, 3, C]"
        )
    _blocked(x[2], "Cin")
    return (x[0], x[1], w[0])


def _conv(xs, attrs, shape):
    x, w = xs
    h, wd, _ = x.shape
    xp = np.zeros((h + 2, wd + 2, x.shape[2]))
    xp[1:-1, 1:-1] = x
    return sum(
        xp[dy : dy + h, dx : dx + wd] @ w[:, dy, dx, :].T
        for dy in range(3)
        for dx in range(3)
    )


OPS.op("conv3x3", 2, _conv_shape, _conv, operands=MX7)


# ------------------------------------------------------------------ mover
def _quantise_shape(shapes, attrs):
    (s,) = shapes
    if not s:
        raise ShapeError("quantise of a scalar")
    _blocked(s[-1], "the last axis")
    return s


OPS.op(
    "quantise",
    1,
    _quantise_shape,
    lambda xs, at, sh: xs[0],
    dtype="mx7",
    operands=("f16",),
)


def _transpose_shape(shapes, attrs):
    (s,) = shapes
    if len(s) < 2:
        raise ShapeError(f"transpose of {fmt_shape(s)}")
    return s[:-2] + (s[-1], s[-2])


OPS.op(
    "transpose",
    1,
    _transpose_shape,
    lambda xs, at, sh: np.swapaxes(xs[0], -1, -2),
    operands=VEC,
)


# ------------------------------------------------------------ vector core
def _ew(fn):
    return lambda xs, at, sh: np.broadcast_to(fn(*xs), sh)


UNARY = {
    "neg": np.negative,
    "abs": np.abs,
    "exp2": np.exp2,
    "log2": np.log2,
    "inv": lambda x: 1.0 / x,
    "rsqrt": lambda x: 1.0 / np.sqrt(x),
    "copy": lambda x: x,
}
BINARY = {
    "add": np.add,
    "sub": np.subtract,
    "mul": np.multiply,
    "max": np.maximum,
    "min": np.minimum,
}
for _name, _fn in UNARY.items():
    OPS.op(_name, 1, broadcast, _ew(_fn), operands=VEC)
for _name, _fn in BINARY.items():
    OPS.op(_name, 2, broadcast, _ew(_fn), operands=VEC)
for _name, _fn in (("lt", np.less), ("gt", np.greater), ("eq", np.equal)):
    OPS.op(f"cmp.{_name}", 2, broadcast, _ew(_fn), dtype="bool", operands=VEC)
OPS.op("fma", 3, broadcast, _ew(lambda a, b, c: a * b + c), operands=VEC)
OPS.op(
    "select",
    3,
    broadcast,
    _ew(lambda p, a, b: np.where(p.astype(bool), a, b)),
    operands=VEC,
)

for _name, _fn in (("sum", np.sum), ("max", np.max), ("min", np.min)):
    OPS.op(
        f"reduce.{_name}",
        1,
        reduced,
        lambda xs, at, sh, _fn=_fn: _fn(xs[0], axis=at.get("axis", -1)),
        attrs=("axis",),
        operands=VEC,
    )


def _fill_shape(shapes, attrs):
    """None: the statement's own shape."""
    if any(shapes):
        raise ShapeError("fill takes a constant")


OPS.op(
    "fill", 1, _fill_shape, lambda xs, at, sh: np.full(sh, xs[0]), operands=("const",)
)


def _gather_shape(shapes, attrs):
    x, idx = shapes
    if len(idx) != 1 or not x:
        raise ShapeError(f"gather of {fmt_shape(x)} at {fmt_shape(idx)}")
    return (idx[0],) + x[1:]


OPS.op(
    "gather",
    2,
    _gather_shape,
    lambda xs, at, sh: xs[0][xs[1].astype(np.int64)],
    operands=VEC,
)


def _scatter_shape(shapes, attrs):
    x, idx, src = shapes
    if len(idx) != 1 or src != (idx[0],) + x[1:]:
        raise ShapeError(
            f"scatter of {fmt_shape(src)} into {fmt_shape(x)} at {fmt_shape(idx)}"
        )
    return x


def _scatter(xs, attrs, shape):
    out = np.array(xs[0], np.float64)
    out[xs[1].astype(np.int64)] = xs[2]
    return out


OPS.op("scatter", 3, _scatter_shape, _scatter, operands=VEC)

__all__ = ["OPS"]
