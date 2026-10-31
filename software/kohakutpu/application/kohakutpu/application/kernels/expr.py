"""The elementwise expression a generated kernel computes, and its lowering
to vector-core ops.

`Load(i)` reads operand i, `Const` is a number, `Op(name, args)` one of
`OPS`, `Reduce(op, x)` folds a row (sum, max, prod). `emit` writes an
expression as DSL values (each op is one vector instruction or a fixed
pattern of them: a square root through `rsqrt`, a sine through a reduction
to [-pi, pi] and a polynomial, a truncation through the rounding constant);
`evaluate` is the same arithmetic in numpy, for references.
"""

import math
from dataclasses import dataclass

import numpy as np

from kohakutpu.language import dsl as kt
from kohakutpu.language.numerics import rounding

#: 1.5 * 2**15: added and subtracted, a lane value |x| < 2**14 rounds to a
#: whole number (E8M15 lanes carry 16 significand bits).
ROUND = 49152.0
TINY = 1e-30
TWO_PI_HI = 6.28125  # exact in few bits, so n * hi stays exact
TWO_PI_LO = 2 * math.pi - 6.28125
#: sin(r) on [-pi, pi], odd Taylor/minimax terms through r^11.
SIN_COEF = (
    1.0,
    -1.0 / 6,
    1.0 / 120,
    -1.0 / 5040,
    1.0 / 362880,
    -1.0 / 39916800,
)


@dataclass(frozen=True)
class Load:
    i: int


@dataclass(frozen=True)
class Const:
    value: float


@dataclass(frozen=True)
class Op:
    name: str
    args: tuple


@dataclass(frozen=True)
class Reduce:
    op: str  # "sum" | "max" | "prod"
    arg: object


#: op -> arity
OPS = {
    "add": 2,
    "sub": 2,
    "mul": 2,
    "div": 2,
    "max": 2,
    "min": 2,
    "pow": 2,
    "neg": 1,
    "abs": 1,
    "recip": 1,
    "exp2": 1,
    "log2": 1,
    "sqrt": 1,
    "rsqrt": 1,
    "sin": 1,
    "trunc": 1,
    "cmplt": 2,
    "cmpgt": 2,
    "cmpeq": 2,
    "cmpne": 2,
    "where": 3,
    "fma": 3,
    "and": 2,
    "or": 2,
    "xor": 2,
    "not": 1,
    "id": 1,
}


def op(name: str, *args) -> Op:
    if name not in OPS:
        raise ValueError(f"no expression op {name!r}")
    if len(args) != OPS[name]:
        raise ValueError(f"{name} takes {OPS[name]} operands, got {len(args)}")
    return Op(
        name,
        tuple(
            a if isinstance(a, (Load, Const, Op, Reduce)) else Const(float(a))
            for a in args
        ),
    )


def loads(e) -> set:
    if isinstance(e, Load):
        return {e.i}
    if isinstance(e, Op):
        return set().union(*(loads(a) for a in e.args))
    if isinstance(e, Reduce):
        return loads(e.arg)
    return set()


def reduces(e) -> list:
    """The outermost reductions in `e`, a node the expression reads twice
    (a mean a layer norm's gradient takes in two places) once."""
    if isinstance(e, Reduce):
        return [e]
    if isinstance(e, Op):
        found = [r for a in e.args for r in reduces(a)]
        return [r for i, r in enumerate(found) if all(r is not q for q in found[:i])]
    return []


def count(e) -> int:
    """Ops in the expression (each node once)."""
    seen: set = set()

    def walk(x):
        if id(x) in seen or not isinstance(x, (Op, Reduce)):
            return
        seen.add(id(x))
        for a in x.args if isinstance(x, Op) else (x.arg,):
            walk(a)

    walk(e)
    return len(seen)


# --------------------------------------------------------------- emission
def emit(e, values: dict, memo: dict | None = None, *, valid_cols=None, carry=False):
    """`e` as DSL values over `values` (operand i -> a kt value, number or
    statistic). A subexpression reached twice is emitted once."""
    memo = {} if memo is None else memo
    key = id(e)
    if key in memo:
        return memo[key]
    if isinstance(e, Load):
        out = values[e.i]
    elif isinstance(e, Const):
        out = float(e.value)
    elif isinstance(e, Reduce):
        out = _reduce(
            e.op,
            emit(e.arg, values, memo, valid_cols=valid_cols, carry=carry),
            valid_cols,
            carry,
        )
    else:
        args = [
            emit(a, values, memo, valid_cols=valid_cols, carry=carry) for a in e.args
        ]
        out = _op(e.name, *args)
    memo[key] = out
    return out


def _num(x) -> bool:
    return isinstance(x, float)


def _op(name, *a):
    if all(_num(x) for x in a):
        return float(_fold(name, *a))
    match name:
        case "add":
            return a[0] + a[1]
        case "sub":
            return a[0] - a[1]
        case "mul":
            return a[0] * a[1]
        case "div":
            return a[0] * (1.0 / a[1]) if _num(a[1]) else a[0] * kt.recip(a[1])
        case "max":
            return kt.maximum(a[0], a[1]) if not _num(a[0]) else kt.maximum(a[1], a[0])
        case "min":
            return kt.minimum(a[0], a[1]) if not _num(a[0]) else kt.minimum(a[1], a[0])
        case "neg":
            return -a[0]
        case "abs":
            return kt.absolute(a[0])
        case "recip":
            return kt.recip(a[0])
        case "exp2":
            return kt.exp2(a[0])
        case "log2":
            return kt.log2(a[0])
        case "rsqrt":
            return kt.rsqrt(a[0])
        case "sqrt":
            # x * rsqrt(x), with 0 kept 0 (rsqrt(0) is infinite).
            return a[0] * kt.rsqrt(kt.maximum(a[0], TINY))
        case "pow":
            base, ex = a
            return (
                kt.exp2(ex * kt.log2(base))
                if not _num(ex)
                else kt.exp2(kt.log2(base) * ex)
            )
        case "sin":
            return _sin(a[0])
        case "trunc":
            return _trunc(a[0])
        case "cmplt":
            return _cmp("cmplt", a[0], a[1])
        case "cmpgt":
            return _cmp("cmpgt", a[0], a[1])
        case "cmpeq":
            return _cmp("cmpeq", a[0], a[1])
        case "cmpne":
            return 1.0 - _cmp("cmpeq", a[0], a[1])
        case "where":
            c, x, y = a
            if _num(c):
                return x if c else y
            return kt.where(c, x, y)
        case "fma":
            return a[0] * a[1] + a[2] if any(_num(x) for x in a) else kt.fma(*a)
        case "and":
            return a[0] * a[1]
        case "or":
            return kt.maximum(a[0], a[1]) if not _num(a[0]) else kt.maximum(a[1], a[0])
        case "xor":
            return kt.absolute(a[0] - a[1])
        case "not":
            return 1.0 - a[0]
        case "id":
            return a[0]
    raise ValueError(f"no lowering for {name}")


def _cmp(name, a, b):
    """A compare of two values or a value and a number; a number first is
    the mirrored compare."""
    if _num(a):
        a, b = b, a
        name = {"cmplt": "cmpgt", "cmpgt": "cmplt", "cmpeq": "cmpeq"}[name]
    return kt.math(name, a, b)


def _round(x):
    """x rounded to the nearest whole number (|x| < 2**14)."""
    return (x + ROUND) - ROUND


def _trunc(x):
    """Toward zero using the traced machine's lane significand width."""
    return rounding.trunc(x, kt, kt.machine().sig_bits)


def _sin(x):
    """sin x: n = round(x / 2pi), r = x - n * 2pi (two-part 2pi), then the
    odd polynomial in r."""
    n = _round(x * (1.0 / (2 * math.pi)))
    r = (x - n * TWO_PI_HI) - n * TWO_PI_LO
    r2 = r * r
    acc = SIN_COEF[-1]
    for c in reversed(SIN_COEF[:-1]):
        acc = r2 * acc + c
    return r * acc


def _reduce(op, x, valid_cols=None, carry=False):
    def fold(kind, value):
        return kt.reduce(kind, value, valid_cols=valid_cols, carry=carry)

    match op:
        case "sum":
            return fold("rsum", x)
        case "max":
            return fold("rmax", x)
        case "prod":
            # sign * 2^sum(log2 |x|), 0 if any element is 0: statistic math
            # holds no compare, so the parity is arithmetic.
            zero = _cmp("cmpeq", x, 0.0)
            neg = _cmp("cmplt", x, 0.0)
            mag = kt.exp2(fold("rsum", kt.log2(kt.absolute(x) + zero)))
            n = fold("rsum", neg)
            half = _round(n * 0.5 - 0.25)
            sign = 1.0 - (n - half * 2.0) * 2.0
            return sign * mag * (1.0 - kt.minimum(fold("rsum", zero), 1.0))
    raise ValueError(f"no row reduction {op!r}")


# --------------------------------------------------------------- numpy
def _fold(name, *a):
    return float(evaluate(Op(name, tuple(Const(x) for x in a)), []))


def evaluate(e, arrays: list):
    """`e` in float64 numpy over `arrays`; a `Reduce` folds the last axis
    (keepdims)."""
    if isinstance(e, Load):
        return np.asarray(arrays[e.i], np.float64)
    if isinstance(e, Const):
        return np.float64(e.value)
    if isinstance(e, Reduce):
        x = evaluate(e.arg, arrays)
        f = {"sum": np.sum, "max": np.max, "prod": np.prod}[e.op]
        return f(x, axis=-1, keepdims=True)
    a = [evaluate(x, arrays) for x in e.args]
    with np.errstate(all="ignore"):
        match e.name:
            case "add":
                return a[0] + a[1]
            case "sub":
                return a[0] - a[1]
            case "mul":
                return a[0] * a[1]
            case "div":
                return a[0] / a[1]
            case "max":
                return np.maximum(a[0], a[1])
            case "min":
                return np.minimum(a[0], a[1])
            case "pow":
                return np.power(a[0], a[1])
            case "neg":
                return -a[0]
            case "abs":
                return np.abs(a[0])
            case "recip":
                return 1.0 / a[0]
            case "exp2":
                return np.exp2(a[0])
            case "log2":
                return np.log2(a[0])
            case "sqrt":
                return np.sqrt(a[0])
            case "rsqrt":
                return 1.0 / np.sqrt(a[0])
            case "sin":
                return np.sin(a[0])
            case "trunc":
                return np.trunc(a[0])
            case "cmplt":
                return (a[0] < a[1]).astype(np.float64)
            case "cmpgt":
                return (a[0] > a[1]).astype(np.float64)
            case "cmpeq":
                return (a[0] == a[1]).astype(np.float64)
            case "cmpne":
                return (a[0] != a[1]).astype(np.float64)
            case "where":
                return np.where(a[0] != 0, a[1], a[2])
            case "fma":
                return a[0] * a[1] + a[2]
            case "and":
                return ((a[0] != 0) & (a[1] != 0)).astype(np.float64)
            case "or":
                return ((a[0] != 0) | (a[1] != 0)).astype(np.float64)
            case "xor":
                return ((a[0] != 0) ^ (a[1] != 0)).astype(np.float64)
            case "not":
                return (a[0] == 0).astype(np.float64)
            case "id":
                return a[0]
    raise ValueError(f"no numpy for {e.name}")


__all__ = [
    "OPS",
    "Const",
    "Load",
    "Op",
    "Reduce",
    "count",
    "emit",
    "evaluate",
    "loads",
    "op",
    "reduces",
]
