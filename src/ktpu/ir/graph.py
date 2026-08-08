"""Level 1: tensors, ops and value semantics. No machine, no tiles, no grid.

Every shape is a concrete integer; specialising a dynamic shape happens above
the IR (docs/driver/ir.md s6.1).
"""

import itertools
from dataclasses import dataclass, field
from enum import Enum

from ktpu.ir.dtype import DType, Kind, converge


class OpKind(Enum):
    INPUT = "input"
    CONST = "const"

    NEG = "neg"
    ABS = "abs"
    RECIP = "recip"
    RSQRT = "rsqrt"
    SQRT = "sqrt"
    EXP2 = "exp2"
    LOG2 = "log2"
    RELU = "relu"

    ADD = "add"
    SUB = "sub"
    MUL = "mul"
    DIV = "div"
    MAX = "max"
    MIN = "min"
    CMPLT = "cmplt"
    CMPGT = "cmpgt"
    CMPEQ = "cmpeq"

    FMA = "fma"
    SELECT = "select"

    SUM = "sum"
    RMAX = "rmax"
    RMIN = "rmin"
    SUMSQ = "sumsq"

    MATMUL = "matmul"

    RESHAPE = "reshape"
    PERMUTE = "permute"
    EXPAND = "expand"
    SLICE = "slice"
    PAD = "pad"
    CONCAT = "concat"

    CAST = "cast"


ELEMENTWISE = frozenset(
    {
        OpKind.NEG,
        OpKind.ABS,
        OpKind.RECIP,
        OpKind.RSQRT,
        OpKind.SQRT,
        OpKind.EXP2,
        OpKind.LOG2,
        OpKind.RELU,
        OpKind.ADD,
        OpKind.SUB,
        OpKind.MUL,
        OpKind.DIV,
        OpKind.MAX,
        OpKind.MIN,
        OpKind.CMPLT,
        OpKind.CMPGT,
        OpKind.CMPEQ,
        OpKind.FMA,
        OpKind.SELECT,
    }
)

REDUCTION = frozenset({OpKind.SUM, OpKind.RMAX, OpKind.RMIN, OpKind.SUMSQ})

VIEW = frozenset(
    {
        OpKind.RESHAPE,
        OpKind.PERMUTE,
        OpKind.EXPAND,
        OpKind.SLICE,
        OpKind.PAD,
        OpKind.CONCAT,
    }
)

ARITY = {
    **{
        k: 1
        for k in (
            OpKind.NEG,
            OpKind.ABS,
            OpKind.RECIP,
            OpKind.RSQRT,
            OpKind.SQRT,
            OpKind.EXP2,
            OpKind.LOG2,
            OpKind.RELU,
            OpKind.CAST,
            *REDUCTION,
            *(VIEW - {OpKind.CONCAT}),
        )
    },
    **{
        k: 2
        for k in (
            OpKind.ADD,
            OpKind.SUB,
            OpKind.MUL,
            OpKind.DIV,
            OpKind.MAX,
            OpKind.MIN,
            OpKind.MATMUL,
            OpKind.CMPLT,
            OpKind.CMPGT,
            OpKind.CMPEQ,
        )
    },
    **{k: 3 for k in (OpKind.FMA, OpKind.SELECT)},
    OpKind.INPUT: 0,
    OpKind.CONST: 0,
}


class ShapeError(ValueError):
    """A shape or dtype disagreement, raised where it happened."""


@dataclass(frozen=True)
class Value:
    """A tensor produced by exactly one op. `vid` identifies it."""

    vid: int
    shape: tuple[int, ...]
    dtype: DType

    def __post_init__(self) -> None:
        if any(d < 0 for d in self.shape):
            raise ShapeError(f"negative extent in {self.shape}")

    @property
    def rank(self) -> int:
        return len(self.shape)

    @property
    def numel(self) -> int:
        n = 1
        for d in self.shape:
            n *= d
        return n

    def __str__(self) -> str:
        dims = "x".join(str(d) for d in self.shape) or "scalar"
        return f"%{self.vid}:{dims}:{self.dtype}"


@dataclass
class Op:
    """One node. `attrs` carries everything that is not a value operand."""

    kind: OpKind
    inputs: tuple[Value, ...]
    out: Value
    attrs: dict = field(default_factory=dict)

    def __str__(self) -> str:
        args = ", ".join(str(v) for v in self.inputs)
        extra = "".join(f" {k}={v!r}" for k, v in sorted(self.attrs.items()))
        return f"{self.out} = {self.kind.value}({args}){extra}"


def broadcast(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[int, ...]:
    """Numpy broadcasting rules, right-aligned. Raises ShapeError if incompatible."""
    out = []
    for i in range(max(len(a), len(b))):
        da = a[len(a) - 1 - i] if i < len(a) else 1
        db = b[len(b) - 1 - i] if i < len(b) else 1
        if da != db and da != 1 and db != 1:
            raise ShapeError(f"cannot broadcast {a} with {b}")
        out.append(max(da, db))
    return tuple(reversed(out))


class Graph:
    """A straight-line list of ops in dependency order, with named inputs and
    outputs. Builder methods validate shapes and dtypes as they append."""

    def __init__(self, name: str = "graph") -> None:
        self.name = name
        self.ops: list[Op] = []
        self.inputs: list[Value] = []
        self.outputs: list[Value] = []
        self._ids = itertools.count()
        self._producer: dict[int, Op] = {}

    def _value(self, shape: tuple[int, ...], dtype: DType) -> Value:
        return Value(next(self._ids), tuple(shape), dtype)

    def _emit(
        self,
        kind: OpKind,
        inputs: tuple[Value, ...],
        shape: tuple[int, ...],
        dtype: DType,
        **attrs,
    ) -> Value:
        want = ARITY[kind]
        if len(inputs) != want:
            raise ShapeError(f"{kind.value} takes {want} operands, got {len(inputs)}")
        out = self._value(shape, dtype)
        op = Op(kind, inputs, out, attrs)
        self.ops.append(op)
        self._producer[out.vid] = op
        return out

    def input(self, shape, dtype: DType, name: str | None = None) -> Value:
        """Declare a graph input and return its value."""
        v = self._emit(OpKind.INPUT, (), tuple(shape), dtype, name=name or "")
        self.inputs.append(v)
        return v

    def const(self, value: float, dtype: DType) -> Value:
        """A scalar constant. Raises ShapeError if it rounds to zero or
        overflows in `dtype`, which would make a written value vanish."""
        if dtype.kind is Kind.FLOAT and value != 0.0:
            mag = abs(value)
            if mag < dtype.smallest_positive / 2.0:
                raise ShapeError(
                    f"the constant {value!r} rounds to zero in {dtype} "
                    f"(smallest is {dtype.smallest_positive:g}); it would "
                    "vanish silently -- scale it, or hold it in a wider dtype"
                )
            if mag > dtype.max_finite:
                raise ShapeError(
                    f"the constant {value!r} overflows {dtype} "
                    f"(largest is {dtype.max_finite:g})"
                )
        return self._emit(OpKind.CONST, (), (), dtype, value=value)

    def elementwise(self, kind: OpKind, *xs: Value) -> Value:
        """Broadcast the operands, converge their dtypes, and emit one value.

        Mixed float formats are legal; float with integer is not.
        """
        if kind not in ELEMENTWISE:
            raise ShapeError(f"{kind.value} is not elementwise")
        out_dtype = converge(*(x.dtype for x in xs))
        shape: tuple[int, ...] = ()
        for x in xs:
            shape = broadcast(shape, x.shape)
        return self._emit(kind, xs, shape, out_dtype)

    def reduce(self, kind: OpKind, x: Value, axes, keepdim: bool = False) -> Value:
        """Reduce `x` over `axes`, dropping them unless `keepdim`."""
        if kind not in REDUCTION:
            raise ShapeError(f"{kind.value} is not a reduction")
        axes = tuple(sorted(a % x.rank for a in axes))
        if len(set(axes)) != len(axes):
            raise ShapeError(f"repeated axis in {axes}")
        shape = tuple(
            (1 if keepdim else None) if i in axes else d for i, d in enumerate(x.shape)
        )
        shape = tuple(d for d in shape if d is not None)
        return self._emit(kind, (x,), shape, x.dtype, axes=axes, keepdim=keepdim)

    def matmul(self, a: Value, b: Value) -> Value:
        """`a @ b` over the last two axes, batch dims broadcast.

        Shapes are M x K and K x N. Mixed operand formats are ordinary --
        FP16 activations against MXFP7 weights is the inference case.
        """
        if a.rank < 2 or b.rank < 2:
            raise ShapeError(f"matmul needs rank >= 2, got {a.shape} and {b.shape}")
        m, ka = a.shape[-2], a.shape[-1]
        kb, n = b.shape[-2], b.shape[-1]
        if ka != kb:
            raise ShapeError(
                f"matmul inner dims disagree: {a.shape} @ {b.shape} "
                f"({ka} vs {kb}) -- shapes are M x K and K x N"
            )
        batch = broadcast(a.shape[:-2], b.shape[:-2])
        return self._emit(
            OpKind.MATMUL, (a, b), (*batch, m, n), converge(a.dtype, b.dtype), k=ka
        )

    def cast(self, x: Value, dtype: DType) -> Value:
        """Convert `x` to `dtype`."""
        return self._emit(OpKind.CAST, (x,), x.shape, dtype, frm=x.dtype.name)

    def reshape(self, x: Value, shape) -> Value:
        """Reinterpret `x` with `shape`, which must have the same element count."""
        shape = tuple(shape)
        n = 1
        for d in shape:
            n *= d
        if n != x.numel:
            raise ShapeError(f"reshape {x.shape} -> {shape} changes element count")
        return self._emit(OpKind.RESHAPE, (x,), shape, x.dtype)

    def permute(self, x: Value, order) -> Value:
        """Reorder axes. `order` must be a permutation of `range(x.rank)`."""
        order = tuple(order)
        if sorted(order) != list(range(x.rank)):
            raise ShapeError(f"permute {order} is not a permutation of rank {x.rank}")
        shape = tuple(x.shape[i] for i in order)
        return self._emit(OpKind.PERMUTE, (x,), shape, x.dtype, order=order)

    def expand(self, x: Value, shape) -> Value:
        """Grow unit axes of `x` to `shape`, keeping rank."""
        shape = tuple(shape)
        if len(shape) != x.rank:
            raise ShapeError(f"expand must keep rank: {x.shape} -> {shape}")
        for old, new in zip(x.shape, shape):
            if old != new and old != 1:
                raise ShapeError(f"cannot expand {x.shape} to {shape}")
        return self._emit(OpKind.EXPAND, (x,), shape, x.dtype)

    def slice(self, x: Value, begin, end) -> Value:
        """Per-axis `[begin, end)`. No step: a strided slice is a gather."""
        begin, end = tuple(begin), tuple(end)
        if len(begin) != x.rank or len(end) != x.rank:
            raise ShapeError(f"slice needs {x.rank} bounds, got {begin} {end}")
        for i, (b, e) in enumerate(zip(begin, end)):
            if not 0 <= b < e <= x.shape[i]:
                raise ShapeError(
                    f"slice axis {i}: [{b}, {e}) is outside [0, {x.shape[i]})"
                )
        shape = tuple(e - b for b, e in zip(begin, end))
        return self._emit(OpKind.SLICE, (x,), shape, x.dtype, begin=begin, end=end)

    def pad(self, x: Value, pads, value: float = 0.0) -> Value:
        """Per-axis `(lo, hi)` padding with `value`."""
        pads = tuple(tuple(p) for p in pads)
        if len(pads) != x.rank:
            raise ShapeError(f"pad needs {x.rank} pairs, got {pads}")
        if any(lo < 0 or hi < 0 for lo, hi in pads):
            raise ShapeError(f"pad amounts must be non-negative, got {pads}")
        shape = tuple(lo + d + hi for d, (lo, hi) in zip(x.shape, pads))
        return self._emit(OpKind.PAD, (x,), shape, x.dtype, pads=pads, value=value)

    def concat(self, xs: list[Value], axis: int = 0) -> Value:
        """Join values along `axis`; every other extent must match.

        Variadic, so ARITY does not apply and `_emit` is bypassed. Raises
        ShapeError on a rank, dtype or extent disagreement.
        """
        if not xs:
            raise ShapeError("concat needs at least one value")
        axis %= xs[0].rank
        dtype = converge(*(x.dtype for x in xs))
        for x in xs[1:]:
            if x.rank != xs[0].rank:
                raise ShapeError(f"concat rank mismatch: {xs[0].shape} vs {x.shape}")
            for i, (a, b) in enumerate(zip(xs[0].shape, x.shape)):
                if i != axis and a != b:
                    raise ShapeError(
                        f"concat axis {i}: {a} != {b} ({xs[0].shape} vs {x.shape})"
                    )
        shape = list(xs[0].shape)
        shape[axis] = sum(x.shape[axis] for x in xs)
        out = self._value(tuple(shape), dtype)
        op = Op(OpKind.CONCAT, tuple(xs), out, {"axis": axis})
        self.ops.append(op)
        self._producer[out.vid] = op
        return out

    def output(self, *vs: Value) -> None:
        """Mark values as graph outputs."""
        self.outputs.extend(vs)

    def producer(self, v: Value) -> Op:
        """The op that produced `v`."""
        return self._producer[v.vid]

    def producer_of(self, vid: int) -> Op:
        """The op that produced value id `vid`. Raises KeyError if unknown."""
        return self._producer[vid]

    def uses(self) -> dict[int, int]:
        """Consumer count per value id. Zero means dead unless it is an output."""
        counts = {op.out.vid: 0 for op in self.ops}
        for op in self.ops:
            for v in op.inputs:
                counts[v.vid] = counts.get(v.vid, 0) + 1
        return counts

    def __str__(self) -> str:
        head = f"graph {self.name}({', '.join(str(v) for v in self.inputs)})"
        body = "\n".join(f"  {op}" for op in self.ops)
        tail = f"  return {', '.join(str(v) for v in self.outputs)}"
        return f"{head}\n{body}\n{tail}"
