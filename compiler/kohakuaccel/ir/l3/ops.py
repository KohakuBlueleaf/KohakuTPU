"""The ops and dtypes an L3 module may name: a project's `OpSet`.

An op is one hardware feature: its arity, the attributes it takes, the rule
for its result's shape (over dims that are ints or symbols) and its numpy
meaning. A dtype is its rounding: every op's result is computed in float64 and
rounded to the dtype its statement names, which is what the hardware stores.
The framework knows no op; it gives the shape rules most ops share.
"""

from dataclasses import dataclass

import numpy as np


class ShapeError(ValueError):
    """Operand shapes an op refuses."""


@dataclass(frozen=True)
class OpDef:
    name: str
    #: Operands taken; None for any number.
    arity: int | None
    #: ``shape(shapes, attrs) -> shape``, or None for the statement's own;
    #: raises `ShapeError`.
    shape: object
    #: ``run(arrays, attrs, shape) -> array`` in float64, before the result's
    #: rounding; `shape` is the statement's.
    run: object
    attrs: tuple = ()
    #: The one dtype its result may have, or None for any.
    dtype: str | None = None
    #: The dtypes an operand may have (the formats its unit reads; ``const``
    #: a literal), or None for any.
    operands: tuple | None = None


class OpSet:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ops: dict = {}
        self.dtypes: dict = {}
        for dt, cast in BASE_DTYPES.items():
            self.dtype(dt, cast)

    def op(self, name, arity, shape, run, attrs=(), dtype=None, operands=None) -> None:
        operands = None if operands is None else tuple(operands)
        self.ops[name] = OpDef(name, arity, shape, run, tuple(attrs), dtype, operands)

    def dtype(self, name: str, cast) -> None:
        """``cast(float64 array) -> float64 array`` of the values `name` holds."""
        self.dtypes[name] = cast


BASE_DTYPES = {
    "f64": lambda x: np.asarray(x, np.float64),
    "f32": lambda x: np.asarray(x, np.float32).astype(np.float64),
    "f16": lambda x: np.asarray(x, np.float16).astype(np.float64),
    "i32": lambda x: np.asarray(x).astype(np.int32).astype(np.int64),
    "bool": lambda x: np.asarray(x).astype(bool),
}


# ------------------------------------------------------------ shape rules
def dim_eq(a, b) -> bool:
    return a == b


def broadcast(shapes, attrs=None) -> tuple:
    """numpy broadcasting, right aligned: a dim of 1 stretches."""
    rank = max((len(s) for s in shapes), default=0)
    out = []
    for k in range(rank):
        dims = [s[len(s) - rank + k] for s in shapes if len(s) - rank + k >= 0]
        big = {d for d in dims if d != 1}
        if len(big) > 1:
            raise ShapeError(
                f"shapes {', '.join(map(fmt_shape, shapes))} do not broadcast"
            )
        out.append(big.pop() if big else 1)
    return tuple(out)


def same(shapes, attrs=None) -> tuple:
    if any(s != shapes[0] for s in shapes):
        raise ShapeError(f"shapes {', '.join(map(fmt_shape, shapes))} differ")
    return shapes[0]


def reduced(shapes, attrs) -> tuple:
    (s,) = shapes
    axis = attrs.get("axis", len(s) - 1)
    if not -len(s) <= axis < len(s):
        raise ShapeError(f"axis {axis} of a rank-{len(s)} value")
    axis %= len(s)
    return s[:axis] + s[axis + 1 :]


def fmt_shape(s) -> str:
    return f"[{', '.join(map(str, s))}]"
