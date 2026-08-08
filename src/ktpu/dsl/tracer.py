"""The traced value, and the five doors it refuses to open.

A `Tracer` looks like a tensor and records what happens to it. `x * x` calls
`__mul__`, which appends a `mul` op and returns a new tracer; at the end the
recorded ops are the graph. Nothing is parsed, so a helper function is inlined
because calling it is calling it, a comprehension unrolls because it runs, and a
constant folds because it was never a tracer to begin with.

What that buys is that only *executed* paths are captured -- which is a trap
unless the unexecuted ones are impossible. Hence the guards: `__bool__`,
`__len__`, `__iter__`, `__index__` and `__float__` raise instead of answering.
Shape and dtype are the exception and are exact: they are known at trace time, so
`x.shape[-1] * 2` is arithmetic on plain ints and needs no graph op at all.

See docs/driver/dsl.md s1-s4.
"""

import math
import numbers

from ktpu.dsl.context import TraceContext
from ktpu.dsl.errors import DSLError, TracerControlFlowError, at_caller
from ktpu.ir import DType, Graph, OpKind, Value

#: Longest mul chain `x ** n` will unroll into. Past this the generic
#: `exp2(n * log2(x))` is used: a graph whose op count grows without bound in a
#: constant is a graph every later pass has to walk, and three ops always beats
#: twenty for a precision difference nobody asked for.
MAX_POWER_UNROLL = 64


class Tracer:
    """A tensor-shaped placeholder that records what is done to it."""

    __slots__ = ("_ctx", "_v")

    # Numpy must not treat a Tracer as an operand. Without this, `arr * x`
    # broadcasts into an object array and calls `__rmul__` once per element --
    # yielding an array of tracers and a graph with one op per element, which is
    # exactly the kind of silently-wrong result docs/driver/dsl.md s3.1 rejects
    # numpy interop to avoid. `None` makes numpy return NotImplemented so Python
    # falls through to `__rmul__`, where the refusal is explicit.
    __array_ufunc__ = None

    def __init__(self, ctx: TraceContext, value: Value) -> None:
        self._ctx = ctx
        self._v = value

    # ---- what IS known at trace time ---------------------------------------

    @property
    def value(self) -> Value:
        """The level-1 value this stands for."""
        return self._v

    @property
    def shape(self) -> tuple[int, ...]:
        """Plain ints, always. `x.shape[-1] * 2` is arithmetic, not an op."""
        return self._v.shape

    @property
    def dtype(self) -> DType:
        return self._v.dtype

    @property
    def rank(self) -> int:
        return self._v.rank

    #: numpy's spelling, because kernels get written by people who know numpy.
    @property
    def ndim(self) -> int:
        return self._v.rank

    @property
    def numel(self) -> int:
        return self._v.numel

    def __str__(self) -> str:
        return str(self._v)

    def __repr__(self) -> str:
        return f"Tracer({self._v}, from {self._origin()})"

    # ---- the guards --------------------------------------------------------
    #
    # Five doors, one rule: a traced value never answers a question about its
    # contents. Each message names the construct, the value, and the way to say
    # the same thing without a branch -- because "TypeError: bool" tells a kernel
    # author nothing about what to do next.

    def __bool__(self) -> bool:
        raise self._reject(
            "bool()",
            "`if`, `while`, `and`, `or`, `not`, `assert` and `in` all ask for it",
            "make the choice a value: where(cond, a, b), or maximum(x, 0.0). "
            "Branching on a plain Python int is free and unrolls.",
        )

    def __len__(self) -> int:
        raise self._reject(
            "len()",
            "it would have to be answered before the machine has run",
            "the rank and every extent ARE known -- use x.shape, "
            "len(x.shape) or x.shape[0].",
        )

    def __iter__(self):
        raise self._reject(
            "iteration",
            "`for`, unpacking, list(), tuple() and `in` all ask for it",
            "loop over range(x.shape[0]) and index with [] if the trip count is "
            "compile-time; otherwise the loop belongs in the reduction, not in "
            "Python.",
        )

    def __index__(self) -> int:
        raise self._reject(
            "use as an index",
            "indexing a Python sequence or a slice bound needs an integer now",
            "an index computed from data is a gather, which is not in the level-1 "
            "op set. Index with a compile-time int.",
        )

    def __int__(self) -> int:
        raise self._reject(
            "int()",
            "it would have to read the value out of the machine",
            "keep the number in Python if it is compile-time; there is no way to "
            "extract one from a graph.",
        )

    def __float__(self) -> float:
        raise self._reject(
            "float()",
            "float(), round(), math.* and %-formatting all ask for it",
            "there is no .item(): a traced value has no contents yet. "
            "docs/driver/dsl.md s3.1.",
        )

    def __array__(self, dtype=None, copy=None):
        raise DSLError(
            f"numpy cannot materialise a traced value, at {at_caller()}\n"
            f"  {self} has a shape and a dtype but no contents until the machine "
            "runs.\n"
            "  Pass tensors in as inputs and constants in as Python scalars."
        )

    # ---- arithmetic --------------------------------------------------------

    def __add__(self, other) -> "Tracer":
        return self._binary(OpKind.ADD, other)

    def __radd__(self, other) -> "Tracer":
        return self._binary(OpKind.ADD, other, flip=True)

    def __sub__(self, other) -> "Tracer":
        return self._binary(OpKind.SUB, other)

    def __rsub__(self, other) -> "Tracer":
        return self._binary(OpKind.SUB, other, flip=True)

    def __mul__(self, other) -> "Tracer":
        return self._binary(OpKind.MUL, other)

    def __rmul__(self, other) -> "Tracer":
        return self._binary(OpKind.MUL, other, flip=True)

    def __truediv__(self, other) -> "Tracer":
        return self._binary(OpKind.DIV, other)

    def __rtruediv__(self, other) -> "Tracer":
        # `1/x` is `recip`, one op and the one the hardware has a seed for
        # (isa/vector.md `VINV`). Emitting div(const 1, x) instead would ask a
        # later peephole to undo a shape the frontend chose for no reason.
        if isinstance(other, numbers.Real) and other == 1:
            return self._wrap(self._elementwise(OpKind.RECIP, self._v))
        return self._binary(OpKind.DIV, other, flip=True)

    def __matmul__(self, other) -> "Tracer":
        return self._contract(other)

    def __neg__(self) -> "Tracer":
        return self._wrap(self._elementwise(OpKind.NEG, self._v))

    def __abs__(self) -> "Tracer":
        return self._wrap(self._elementwise(OpKind.ABS, self._v))

    def __pos__(self) -> "Tracer":
        return self

    def __pow__(self, other) -> "Tracer":
        """`x ** n`, expanded at trace time; no `pow` op exists or is wanted."""
        match other:
            case int() if 1 <= other <= MAX_POWER_UNROLL:
                return _int_power(self, other)
            case int() if -MAX_POWER_UNROLL <= other <= -1:
                return 1.0 / _int_power(self, -other)
            case float() if other == 0.5:
                return self._wrap(self._elementwise(OpKind.SQRT, self._v))
            case float() if other == -0.5:
                return self._wrap(self._elementwise(OpKind.RSQRT, self._v))
        # Everything else, including a traced exponent: 2^(n * log2 x). Both
        # halves are single-pass on the vector core, so this is cheap as well as
        # general -- and it is what a library `pow` would do anyway.
        return _exp2(_log2(self) * other)

    def __rpow__(self, other) -> "Tracer":
        # `2 ** x` is exp2 exactly, and exp2 is the op the hardware has
        # (isa/vector.md `VEXP2`, one pass). Any other base folds its log2 into a
        # constant at trace time, which is the whole base-2 argument in
        # docs/compute/vector-core.md s4.1.
        if not isinstance(other, numbers.Real) or other <= 0:
            raise DSLError(
                f"base ** traced needs a positive number as the base, got "
                f"{other!r}, at {at_caller()}"
            )
        return _exp2(self if other == 2 else self * math.log2(other))

    # ---- comparison, which produces a mask ---------------------------------
    # `<`, `>` and `==` are one op each, matching VCMPLT/VCMPGT/VCMPEQ. The
    # negated three cost two, since a mask is 1.0 or 0.0 and `1 - m` inverts it.

    def __lt__(self, other) -> "Tracer":
        return self._binary(OpKind.CMPLT, other)

    def __gt__(self, other) -> "Tracer":
        return self._binary(OpKind.CMPGT, other)

    def __eq__(self, other) -> "Tracer":
        return self._binary(OpKind.CMPEQ, other)

    def __ge__(self, other) -> "Tracer":
        return 1.0 - self._binary(OpKind.CMPLT, other)

    def __le__(self, other) -> "Tracer":
        return 1.0 - self._binary(OpKind.CMPGT, other)

    def __ne__(self, other) -> "Tracer":
        return 1.0 - self._binary(OpKind.CMPEQ, other)

    # `__eq__` is elementwise, so it cannot answer "is this the same object" and
    # Python would otherwise make the class unhashable. Identity is the only
    # honest hash a value with no contents can have.
    __hash__ = object.__hash__

    # ---- reductions --------------------------------------------------------

    def sum(self, axis=None, keepdim: bool = False) -> "Tracer":
        return self._reduce(OpKind.SUM, axis, keepdim)

    def max(self, axis=None, keepdim: bool = False) -> "Tracer":
        return self._reduce(OpKind.RMAX, axis, keepdim)

    def min(self, axis=None, keepdim: bool = False) -> "Tracer":
        return self._reduce(OpKind.RMIN, axis, keepdim)

    def sumsq(self, axis=None, keepdim: bool = False) -> "Tracer":
        """Sum of squares in one op -- the tree node is an FMA, so `a*a+c` is free."""
        return self._reduce(OpKind.SUMSQ, axis, keepdim)

    def mean(self, axis=None, keepdim: bool = False) -> "Tracer":
        """Sugar: `sum * (1/n)`. Leaves no trace of itself in the graph."""
        return self._reduce(OpKind.SUM, axis, keepdim) * (1.0 / self._count(axis))

    def var(self, axis=None, keepdim: bool = False, correction: int = 0) -> "Tracer":
        """Sugar, in the two-pass form: `mean((x - mean x)^2)`.

        NOT `E[x^2] - E[x]^2`, which is one pass cheaper and wrong here. The
        vector core computes in E8M15 -- 16 significand bits -- and that identity
        cancels catastrophically when |mean| exceeds the spread, which is exactly
        the distribution a LayerNorm sees after a residual add.
        """
        deviation = self - self.mean(axis, keepdim=True)
        total = (deviation * deviation)._reduce(OpKind.SUM, axis, keepdim)
        return total * (1.0 / (self._count(axis) - correction))

    # ---- views and casts ---------------------------------------------------

    def reshape(self, *shape) -> "Tracer":
        return self._wrap(self._ctx.emit(self._graph.reshape, self._v, _dims(shape)))

    def permute(self, *order) -> "Tracer":
        return self._wrap(self._ctx.emit(self._graph.permute, self._v, _dims(order)))

    def transpose(self, a: int = -2, b: int = -1) -> "Tracer":
        """Swap two axes. The default is the one a matmul wants."""
        order = list(range(self.rank))
        order[a], order[b] = order[b], order[a]
        return self.permute(order)

    def expand(self, *shape) -> "Tracer":
        """Grow unit axes. Rank-preserving, as `Graph.expand` requires."""
        return self._wrap(self._ctx.emit(self._graph.expand, self._v, _dims(shape)))

    def broadcast_to(self, *shape) -> "Tracer":
        """`expand` that may also add leading axes, by reshaping first."""
        want = _dims(shape)
        if len(want) < self.rank:
            raise DSLError(
                f"broadcast_to cannot drop axes: {self.shape} -> {want}, at "
                f"{at_caller()}"
            )
        lifted = self.reshape((1,) * (len(want) - self.rank) + self.shape)
        return lifted.expand(want)

    def to(self, dtype: DType) -> "Tracer":
        """The only way across dtypes: mixed-dtype arithmetic is an error."""
        return self._wrap(self._ctx.emit(self._graph.cast, self._v, dtype))

    def pad(self, pads, value: float = 0.0) -> "Tracer":
        """`pads` is `(lo, hi)` per axis, or one int applied to every edge."""
        widths = _pad_widths(pads, self.rank)
        shape = tuple(lo + d + hi for d, (lo, hi) in zip(self.shape, widths))
        return self._view(OpKind.PAD, shape, pads=widths, value=float(value))

    def __getitem__(self, key) -> "Tracer":
        keys = key if isinstance(key, tuple) else (key,)
        if any(isinstance(k, Tracer) for k in keys):
            raise TracerControlFlowError(
                f"indexing with a traced value, at {at_caller()}\n"
                "  An index computed from data is a gather, and the level-1 op "
                "set has none (docs/driver/ir.md s2.1).\n"
                "  Select with a value instead: where(cond, a, b)."
            )
        keys = _fill_ellipsis(keys, self.rank, self.shape)
        begin, end, drop = [], [], []
        for axis, (k, extent) in enumerate(zip(keys, self.shape)):
            match k:
                case int():
                    index = k + extent if k < 0 else k
                    if not 0 <= index < extent:
                        raise DSLError(
                            f"index {k} is out of range for axis {axis} of "
                            f"{self.shape}, at {at_caller()}"
                        )
                    begin.append(index)
                    end.append(index + 1)
                    drop.append(axis)
                case slice():
                    if k.step not in (None, 1):
                        raise DSLError(
                            f"a strided slice is a gather, not a view, at "
                            f"{at_caller()}\n  step={k.step} on axis {axis}."
                        )
                    lo, hi, _ = k.indices(extent)
                    begin.append(lo)
                    end.append(max(lo, hi))
                case _:
                    raise DSLError(
                        f"cannot index a traced value with "
                        f"{type(k).__name__}, at {at_caller()}\n"
                        "  Use ints, slices with unit step, and Ellipsis."
                    )
        shape = tuple(hi - lo for lo, hi in zip(begin, end))
        out = self
        if shape != self.shape:
            out = self._view(OpKind.SLICE, shape, begin=tuple(begin), end=tuple(end))
        if drop:
            out = out.reshape(tuple(d for i, d in enumerate(shape) if i not in drop))
        return out

    # ---- machinery ---------------------------------------------------------

    @property
    def _graph(self) -> Graph:
        return self._ctx.graph

    def _wrap(self, value: Value) -> "Tracer":
        return Tracer(self._ctx, value)

    def _elementwise(self, kind: OpKind, *xs: Value) -> Value:
        return self._ctx.emit(self._graph.elementwise, kind, *xs)

    def _binary(self, kind: OpKind, other, *, flip: bool = False) -> "Tracer":
        rhs = self._operand(other)
        xs = (rhs, self._v) if flip else (self._v, rhs)
        return self._wrap(self._elementwise(kind, *xs))

    def _contract(self, other) -> "Tracer":
        if not isinstance(other, Tracer):
            raise DSLError(
                f"dot needs two traced values, at {at_caller()}\n"
                f"  got {type(other).__name__} on the right. A weight is an "
                "input to the kernel, not a constant folded into it."
            )
        self._same_trace(other)
        return self._wrap(self._ctx.emit(self._graph.matmul, self._v, other._v))

    def _operand(self, other) -> Value:
        """Coerce the other side of a binary op into a value in this dtype."""
        match other:
            case Tracer():
                self._same_trace(other)
                return other._v
            case _ if isinstance(other, numbers.Real):
                # A Python scalar adopts the other side's dtype rather than
                # forcing one. It has no format of its own, and giving it FP32 by
                # default would widen every `x * 2.0` on an FP16 tensor into an
                # FP32 value through `converge` -- a conversion nobody wrote.
                #
                # The cost is the other direction: a scalar too small for the
                # operand's format rounds toward zero here and nothing says so.
                # `eps=1e-6` against an FP16 tensor is subnormal. Cast the tensor
                # if that matters; catching it belongs in `Graph.const`, which is
                # where the rounding actually happens.
                #
                # `numbers.Real` rather than `int | float` so a numpy scalar --
                # np.float32(2.0), which is NOT a Python float although
                # np.float64 is -- does not get one answer on one machine word
                # and a refusal on another. A numpy ARRAY is still refused below.
                return self._ctx.const(float(other), self.dtype)
            case _:
                raise DSLError(
                    f"cannot combine a traced value with {type(other).__name__}, "
                    f"at {at_caller()}\n"
                    "  Traced values combine with other traced values from the "
                    "same trace, or with plain Python ints and floats. Arrays go "
                    "in as kernel inputs."
                )

    def _same_trace(self, other: "Tracer") -> None:
        if other._ctx is not self._ctx:
            raise DSLError(
                f"these two traced values come from different traces, at "
                f"{at_caller()}\n"
                f"  {self} and {other}. A Tracer belongs to the one call that "
                "created it; carrying one out in a global or a closure produces "
                "a graph with an edge to a value it does not contain."
            )

    def _view(self, kind: OpKind, shape: tuple[int, ...], **attrs) -> "Tracer":
        # One path for every view op; the shape is already computed by the
        # caller, so the Graph builders' own checks would be redundant.
        return self._wrap(
            self._ctx.emit(
                self._graph._emit, kind, (self._v,), shape, self.dtype, **attrs
            )
        )

    def concat(self, *others, axis: int = 0) -> "Tracer":
        """Join this tracer with `others` along `axis`.

        Every other extent must match. Returns a new Tracer; raises
        TracerShapeError on a rank or extent disagreement.
        """
        for o in others:
            self._same_trace(o)
        vals = [self._v] + [o._v for o in others]
        return self._wrap(self._ctx.emit(self._graph.concat, vals, axis))

    def _axes(self, axis) -> tuple[int, ...]:
        if axis is None:
            return tuple(range(self.rank))
        if isinstance(axis, int):
            return (axis,)
        return tuple(axis)

    def _count(self, axis) -> int:
        """How many elements a reduction over `axis` folds together."""
        n = 1
        for a in self._axes(axis):
            n *= self.shape[a % self.rank] if self.rank else 1
        return n

    def _reduce(self, kind: OpKind, axis, keepdim: bool) -> "Tracer":
        return self._wrap(
            self._ctx.emit(self._graph.reduce, kind, self._v, self._axes(axis), keepdim)
        )

    def _origin(self) -> str:
        op = self._graph.producer(self._v)
        if op.kind is OpKind.INPUT:
            return f"input {op.attrs.get('name') or '?'!r}"
        return op.kind.value

    def _reject(self, construct: str, why: str, advice: str) -> TracerControlFlowError:
        return TracerControlFlowError(
            f"{construct} on a traced value, at {at_caller()}\n"
            f"  {self} is data-dependent -- {self._origin()} produced it, and its "
            "contents do not exist until the machine runs, so this cannot be "
            "answered at trace time.\n"
            f"  {why}.\n"
            f"  {advice}  (docs/driver/dsl.md s2)"
        )


def unary(kind: OpKind, x) -> "Tracer":
    """One-operand elementwise op, for the free functions in `ops`."""
    if not isinstance(x, Tracer):
        raise DSLError(
            f"{kind.value} needs a traced value, got {type(x).__name__}, at "
            f"{at_caller()}\n"
            "  A compile-time number is ordinary Python -- use the math module."
        )
    return x._wrap(x._elementwise(kind, x._v))


def binary(kind: OpKind, a, b) -> "Tracer":
    """Two-operand elementwise op, keeping `(a, b)` as the operand order."""
    if isinstance(a, Tracer):
        return a._binary(kind, b)
    if isinstance(b, Tracer):
        return b._binary(kind, a, flip=True)
    raise DSLError(
        f"{kind.value} needs at least one traced value, at {at_caller()}\n"
        f"  got {type(a).__name__} and {type(b).__name__}."
    )


def ternary(kind: OpKind, a, b, c) -> "Tracer":
    """Three-operand elementwise op. The first operand must be traced."""
    if not isinstance(a, Tracer):
        raise DSLError(
            f"{kind.value} needs its first operand traced, got "
            f"{type(a).__name__}, at {at_caller()}"
        )
    return a._wrap(a._elementwise(kind, a._v, a._operand(b), a._operand(c)))


def _exp2(x: Tracer) -> "Tracer":
    return unary(OpKind.EXP2, x)


def _log2(x: Tracer) -> "Tracer":
    return unary(OpKind.LOG2, x)


def _int_power(x: Tracer, n: int) -> "Tracer":
    """`x ** n` for n >= 1, by square-and-multiply -- 2*log2(n) muls at worst."""
    result: Tracer | None = None
    base = x
    while n:
        if n & 1:
            result = base if result is None else result * base
        n >>= 1
        if n:
            base = base * base
    return result


def _dims(shape) -> tuple[int, ...]:
    """Accept `f(2, 16)` and `f((2, 16))` alike, as numpy does."""
    if len(shape) == 1 and not isinstance(shape[0], int):
        return tuple(shape[0])
    return tuple(shape)


def _fill_ellipsis(keys: tuple, rank: int, shape: tuple[int, ...]) -> tuple:
    n_ellipsis = sum(1 for k in keys if k is Ellipsis)
    if n_ellipsis > 1:
        raise DSLError(f"more than one Ellipsis in an index, at {at_caller()}")
    if n_ellipsis:
        at = keys.index(Ellipsis)
        fill = rank - (len(keys) - 1)
        keys = keys[:at] + (slice(None),) * fill + keys[at + 1 :]
    if len(keys) > rank:
        raise DSLError(
            f"{len(keys)} indices for a rank-{rank} value {shape}, at {at_caller()}"
        )
    return keys + (slice(None),) * (rank - len(keys))


def _pad_widths(pads, rank: int) -> tuple[tuple[int, int], ...]:
    if isinstance(pads, int):
        return ((pads, pads),) * rank
    widths = tuple((int(lo), int(hi)) for lo, hi in pads)
    if len(widths) != rank:
        raise DSLError(
            f"pad wants one (lo, hi) per axis: {len(widths)} for rank {rank}, at "
            f"{at_caller()}"
        )
    if any(lo < 0 or hi < 0 for lo, hi in widths):
        raise DSLError(f"negative pad width in {widths}, at {at_caller()}")
    return widths
