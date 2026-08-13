"""What a vector core's statements mean: one expression over lanes.

A vector core takes an instruction image, descriptors and one RUN per batch, so
its vocabulary is an expression rather than a sweep.

A CHAIN IS LINEAR, AN EXPRESSION IS NOT: one running result register, so a node
with two computed arguments lifts one of them into a chain of its own and reads
the result from a register. A band runs those chains in order, so the whole
expression is still ONE program and the lifted values never reach memory.
"""

from dataclasses import dataclass
from typing import Self

from kohakuaccel.lang import Dim, active
from kohakutpu.hw.ops import OpKind
from kohakutpu.isa.vecemit import OUT_REG, forward
from kohakutpu.lang.errors import LangError

#: Statement kinds this unit runs.
KINDS = {"apply", "reduce"}


@dataclass(frozen=True)
class Ref:
    """A leaf: one PART of one buffer's elements.

    The part is carried per leaf, not per statement, so one expression can read
    one block of a long buffer while writing another.
    """

    name: str
    part: object = 0
    #: Extra offset in ELEMENTS, from slicing the buffer. A part is a whole
    #: `part`-sized step, which a block boundary need not land on.
    off: object = 0
    #: Elements between one group start and the next, and the elements read at
    #: each. Zero is the contiguous read every leaf but a spread makes.
    period: object = 0
    take: object = 0

    def rebind(self, resolve) -> "Ref":
        """This leaf with its part, offset and period resolved to numbers."""
        return Ref(
            self.name,
            resolve(self.part),
            resolve(self.off),
            resolve(self.period),
            resolve(self.take),
        )


@dataclass(frozen=True)
class Resident:
    """A leaf: the cluster's resident tile, handed over rather than read back.

    A `DRAIN` may address a NoC node instead of memory, so the accumulator
    arrives in this core's L1 as `gm*gn` words of FP16 sub-tiles -- the same
    bytes a memory drain would have written.
    """

    gm: int
    gn: int
    #: Which accumulator. Two tiles of the same shape are otherwise equal, and
    #: `leaves_of` would fold them into one leaf and read the wrong one twice.
    at: str = ""

    @property
    def words(self) -> int:
        """L1 words it occupies: one 256-bit word per 4x4 FP16 sub-tile."""
        return self.gm * self.gn


#: `at` -> the `cluster.Tile`, for an op sub-tile order cannot serve. NOT a
#: field of `Resident`: a live object on that leaf cost 112 flits -> 150.
TILES: dict = {}


@dataclass(frozen=True)
class Const:
    """A leaf: one scalar, which the compiler materialises as an operand.

    `value` may be a symbolic extent, resolved when the constant is
    materialised.
    """

    value: object


@dataclass(frozen=True)
class Node:
    """One operation over leaves and other nodes."""

    kind: OpKind
    args: tuple


@dataclass(frozen=True)
class Fold(Node):
    """A reduction inside an expression, carrying the row it folds.

    `VRED` folds exactly VL lanes, so the row shape travels with the node.
    Without it a band would step VLMAX over a 64-wide row, fold two rows into
    one answer, and report success.
    """

    rows: object = 0
    cols: object = 0


def rows_cols(operand):
    """The ``(rows, cols)`` an operand's elements sit in, or None.

    A reduction runs ALONG a row, so a fold inside an expression needs the row
    width of what it folds -- which only the buffer underneath it knows.
    """
    held = getattr(operand, "shape_rc", None)
    if held is not None:
        return held
    trailing = getattr(operand, "trailing", ())
    return (trailing[0], trailing[-1]) if len(trailing) >= 2 else None


def expr_of(operand):
    """An operand as an expression node.

    A whole buffer stands for all of its elements. Read by attribute, not by
    class, so an accumulator can offer itself as a leaf.
    """
    held = getattr(operand, "expr", None)
    if held is not None:
        return held
    whole = getattr(operand, "whole", None)
    if whole is not None:
        return whole().expr
    if isinstance(operand, Dim):
        return Const(operand)
    return Const(float(operand))


class Value:
    """An expression over lanes. Arithmetic on it builds the chain."""

    def __init__(self, expr, shape_rc=None) -> None:
        self.expr = expr
        #: The ``(rows, cols)`` the elements sit in, carried from the buffer at
        #: the bottom so a fold written INSIDE an expression knows its row.
        self.shape_rc = shape_rc

    def _op(self, kind: OpKind, other=None, flip: bool = False) -> "Value":
        if other is None:
            return Value(Node(kind, (self.expr,)), self.shape_rc)
        rhs = expr_of(other)
        pair = (rhs, self.expr) if flip else (self.expr, rhs)
        return Value(Node(kind, pair), self.shape_rc or rows_cols(other))

    def __mul__(self, other):
        return self._op(OpKind.MUL, other)

    def __rmul__(self, other):
        return self._op(OpKind.MUL, other, flip=True)

    def __add__(self, other):
        return self._op(OpKind.ADD, other)

    def __radd__(self, other):
        return self._op(OpKind.ADD, other, flip=True)

    def __sub__(self, other):
        return self._op(OpKind.SUB, other)

    def __rsub__(self, other):
        return self._op(OpKind.SUB, other, flip=True)

    def __truediv__(self, other):
        return self._op(OpKind.DIV, other)

    def __rtruediv__(self, other):
        return self._op(OpKind.DIV, other, flip=True)

    def __neg__(self):
        return self._op(OpKind.NEG)

    def __iadd__(self, other):
        """Always raises :class:`LangError`: this name is no longer a tile.

        `acc = acc * corr` REBINDS `acc` to an expression, so the `acc += ...`
        after it sweeps into something that is not an accumulator. Without this
        the chain reaches `float()` and reports a bare `TypeError`.
        """
        raise LangError(
            "this name holds an EXPRESSION, not an accumulator: an earlier line "
            "rebound it with arithmetic, and `+=` on the result is not a sweep. "
            "Keep the accumulator in its own name and write the arithmetic where "
            "it is used, as `y[i, j] <<= f(acc)`"
        )


def exp2(x: Value) -> Value:
    """2**x -- the transcendental this core has.

    Fold `log2 e` into whatever produced `x`; multiplying here costs a whole
    vector pass over the operand.
    """
    return x._op(OpKind.EXP2)


def log2(x: Value) -> Value:
    return x._op(OpKind.LOG2)


def recip(x: Value) -> Value:
    return x._op(OpKind.RECIP)


def rsqrt(x: Value) -> Value:
    return x._op(OpKind.RSQRT)


def sqrt_approx(x: Value) -> Value:
    """``sqrt(x)`` as ``1/(1/sqrt(x))``: one VRSQRT, one VINV, two words.

    This ALU has no square root, so a root is composed -- and this composes the
    two SEEDS, paying both their table errors.

    MEASURED at 1.556 ulp of E8M15 against 0.500 for a correctly rounded root,
    by `scripts/py/sqrt_paths.py`. No `SimDevice` test can see that: the model
    computes VRSQRT and VINV as `1/np.sqrt(a)` and `1/a` EXACTLY, so it reports
    this path as perfect. The script evaluates the seeds as the RTL does.

    Carries zero and infinity correctly, which `x * rsqrt(x)` does not -- that
    form is NaN at both and buys 0.05 ulp for it.
    """
    return recip(rsqrt(x))


def sqrt_newton(x: Value) -> Value:
    """``sqrt(x)`` with one Newton step on the reciprocal root: six words.

    The correction is VMUL and VSUB alone, so no second approximation enters it
    and the fixed point is the true root to within lane rounding.

    MEASURED at 1.467 ulp against :func:`sqrt_approx`'s 1.556 -- **0.09 ulp for
    four more words**. The seed is already within 0.07 ulp of correctly rounded,
    so the correction's own five roundings cost about what the seed error it
    removes was worth. Prefer :func:`sqrt_approx` unless a later step amplifies.

    `s` and `y` are each read twice, so a band holds two intermediates in
    registers. A FUSED EPILOGUE cannot run this: `chain_of` keeps ONE running
    result and refuses a lifted subexpression.
    """
    y = rsqrt(x)
    s = x * y
    return s * (1.5 - s * y * 0.5)


def absolute(x: Value) -> Value:
    return x._op(OpKind.ABS)


#: What a MULTI-operand operator accepts. The unary ops take a `Value` and mean
#: it -- the asymmetry is deliberate, not an oversight.
Operand = Value | float | int


def maximum(a: Operand, b: Operand) -> Value:
    """Elementwise max, ONE word. `vec_alu.v:144` selects on `s1_a` vs `s1_b`."""
    return _binary(OpKind.MAX, a, b)


def minimum(a: Operand, b: Operand) -> Value:
    """Elementwise min, one word -- `vec_alu.v:145`."""
    return _binary(OpKind.MIN, a, b)


def less(a: Operand, b: Operand) -> Value:
    """``a < b``, usable ONLY as the condition of :func:`where`.

    It writes a predicate register and no vector register, so the compare and
    the select it feeds are lowered together -- `vecemit.fuse_compares`.
    """
    return _binary(OpKind.CMPLT, a, b)


def greater(a: Operand, b: Operand) -> Value:
    """``a > b``, usable only as a :func:`where` condition. See :func:`less`."""
    return _binary(OpKind.CMPGT, a, b)


def equal(a: Operand, b: Operand) -> Value:
    """``a == b``, a :func:`where` condition. NaN compares unequal -- `vec_alu.v:127`."""
    return _binary(OpKind.CMPEQ, a, b)


def where(cond: Operand, on_true: Operand, on_false: Operand) -> Value:
    """`on_true` where `cond` is non-zero, else `on_false`.

    Both sides are evaluated: a lane has no branch. The operand order is pinned
    HERE and `lower_op` places the predicate in `vc`.
    """
    node = Node(OpKind.SELECT, (expr_of(cond), expr_of(on_true), expr_of(on_false)))
    return Value(node, rows_cols(on_true) or rows_cols(cond))


def fma(a: Operand, b: Operand, c: Operand) -> Value:
    """``a * b + c`` in one word, so the rounding is the hardware's single one."""
    node = Node(OpKind.FMA, (expr_of(a), expr_of(b), expr_of(c)))
    return Value(node, rows_cols(a) or rows_cols(b))


def _binary(kind: OpKind, a: Operand, b: Operand) -> Value:
    """One two-operand node, whichever side carries the row shape."""
    if isinstance(a, Value):
        return a._op(kind, b)
    if isinstance(b, Value):
        return b._op(kind, a, flip=True)
    return Value(Node(kind, (expr_of(a), expr_of(b))))


class Reduction(Value):
    """A row-wise reduction, and a VALUE the rest of the algebra composes with.

    `kind` is an L1 tree mode rather than an elementwise op, and `part` is what
    it folds -- kept beside the expression so a bare `y[e] <<= row_sum(x[e])`
    still lowers to the reduction pass it always did.
    """

    def __init__(self, kind: OpKind, part) -> None:
        shape = rows_cols(part)
        held = expr_of(part)
        node = Fold(kind, (held,), *shape) if shape else Node(kind, (held,))
        super().__init__(node, shape)
        self.kind, self.part = kind, part

    @property
    def source(self):
        """The buffer this folds, or None when it folds a computed expression.

        A reduction runs ALONG a row, so only a named buffer can be handed to a
        pass of its own; anything else is an operation inside a chain.
        """
        held = getattr(self.part, "buffer", self.part)
        return held if getattr(held, "name", None) else None


def _rowwise(kind: OpKind, part) -> Reduction:
    """A row reduction. An accumulator is LANDED first, and may be.

    A drained tile is 4x4 sub-tiles with four logical rows to a 32-byte word,
    so no row of it is addressable however the sweep was tiled. `Tile.landed`
    emits the drain and the layout pass inserts the sub-tile to row order
    conversion -- the one link `planned_btb.py`'s `sc_link` also pays.
    """
    held = getattr(part, "expr", None)
    if isinstance(held, Resident):
        tile = TILES.get(held.at)
        if tile is None:
            raise LangError(
                "this row reduction reads an accumulator that cannot land: the "
                "tile it came from is not available to drain"
            )
        part = tile.landed()
    return Reduction(kind, part)


def row_max(part) -> Reduction:
    """The maximum of each row, broadcast back across it."""
    return _rowwise(OpKind.RMAX, part)


def row_sum(part) -> Reduction:
    """The sum of each row, broadcast back across it."""
    return _rowwise(OpKind.SUM, part)


class Part(Value):
    """One index of a buffer: this instance's part of its elements."""

    def __init__(
        self, buffer, index, off=0, grid=None, names=(), period=0, take=0, top=False
    ) -> None:
        super().__init__(Ref(buffer.name, index, off, period, take), rows_cols(buffer))
        self.buffer, self.index, self.off = buffer, index, off
        #: Set when this part was made by a whole-buffer write, which knows the
        #: extent it needs and so can be staged by the compiler.
        self.grid, self.names = grid, names
        #: Runs AFTER the tiling being recorded, not inside it -- a staged
        #: epilogue covers the whole result, not one tile of it.
        self.top = top

    def __ilshift__(self, value) -> Self:
        """`y[i] <<= expr` -- evaluate an expression into this part."""
        return self.write(value)

    def write(self, value) -> Self:
        """The same as `<<=`, callable where Python forbids an augmented store."""
        if isinstance(value, Reduction) and value.source is not None:
            return self._reduce(value)
        if isinstance(value, (int, float)):
            # There is no store-immediate: a lane is written by the chain, so a
            # constant is this buffer multiplied away and the value added back.
            value = self * 0.0 + float(value)
        if not isinstance(value, Value):
            raise LangError("a vector result is written from an expression")
        if not isinstance(value.expr, Node):
            # There is no move either: a copy is the chain multiplying by one.
            value = value * 1.0
        leaves = leaves_of(value.expr)
        lifted, chain = chains_of(value.expr, leaves)
        folded = fold_shape(value.expr)
        active().emit(
            "apply",
            grid=self.grid,
            names=self.names,
            top=self.top,
            writes=self.buffer.name,
            reads=tuple(le.name for le in leaves if isinstance(le, Ref)),
            result=self.buffer.name,
            part=self.index,
            off=self.off,
            chain=tuple(chain),
            # The chains this one takes from a register, in run order. Empty
            # for every expression that is already linear.
            lifted=tuple(tuple(ops) for ops in lifted),
            rows=folded[0] if folded else None,
            cols=folded[1] if folded else None,
            leaves=tuple(leaves),
        )
        return self

    def _reduce(self, red: Reduction) -> Self:
        """`y[i] <<= row_max(x[i])` -- one TREE-mode pass over whole rows.

        A reduction runs ALONG a row, so the operand must be in true row order
        and this pass covers the whole block rather than a `part` of it.
        """
        # `row_max(x)` takes a whole buffer as readily as one part of one.
        source = red.source
        active().emit(
            "reduce",
            # A reduction covers whole rows in one pass, so its grid is one
            # instance whatever the elementwise work around it is split into.
            grid=(1,) if self.grid else None,
            names=self.names,
            writes=self.buffer.name,
            reads=(source.name,),
            result=self.buffer.name,
            source=source.name,
            fold=red.kind,
            rows=source.rows,
            cols=source.cols,
            # The shape an `apply` carries too, so a band can hold this beside
            # one instead of the reduction always being a program of its own.
            part=self.index,
            off=self.off,
            chain=((red.kind, [0]),),
            leaves=(red.expr.args[0],),
        )
        return self


def leaves_of(expr, out: list | None = None) -> list:
    """Every buffer and constant in the expression, first-encounter order."""
    out = [] if out is None else out
    if isinstance(expr, Node):
        for arg in expr.args:
            leaves_of(arg, out)
    elif expr not in out:
        out.append(expr)
    return out


def chains_of(expr, leaves: list) -> tuple[list, list]:
    """``(lifted, final)`` -- the chains this expression lowers to, in run order.

    A chain is linear because the core keeps ONE running result, so a node with
    two computed arguments, and any subexpression several places read, becomes a
    chain of its own. A source naming chain `k` of `lifted` is :func:`forward`'s
    index; a band holds that result in a register, so it never reaches memory.
    """
    lifted: list = []
    held: list = []
    final = _linear(expr, leaves, _shared(expr), lifted, held)
    return lifted, final


def chain_of(expr, leaves: list) -> list:
    """A LINEAR chain of ``(OpKind, source registers)``.

    Raises :class:`LangError` for an expression needing more than one chain: a
    fused epilogue is one image with one running result and has nowhere to hand
    a second value to.
    """
    lifted, final = chains_of(expr, leaves)
    if lifted:
        raise LangError(
            f"{expr.kind.value} has two computed arguments and this pass keeps "
            f"ONE running result, so the second has nowhere to wait; lift it "
            f"into its own statement"
        )
    return final


def _linear(expr, leaves: list, shared: list, lifted: list, held: list) -> list:
    """`expr` as ONE chain, lifting whatever cannot run inside it.

    At most one computed argument stays in the chain and writes the running
    result; every other one is lifted, and so is anything `shared` names, which
    is what makes a repeated subexpression run once.
    """
    free = [a for a in expr.args if isinstance(a, Node) and _at(shared, a) is None]
    inline = free[0] if free else None
    pre: list = []
    srcs: list = []
    for arg in expr.args:
        if arg is inline:
            pre = _linear(arg, leaves, shared, lifted, held)
            srcs.append(OUT_REG)
        elif isinstance(arg, Node):
            srcs.append(forward(_lift(arg, leaves, shared, lifted, held)))
        else:
            srcs.append(leaves.index(arg))
    return [*pre, (expr.kind, srcs)]


def _lift(node, leaves: list, shared: list, lifted: list, held: list) -> int:
    """The index of `node`'s own chain, building it on first encounter.

    Its own dependencies are lifted first, so `lifted` is in run order and a
    chain never forwards one that has not run.
    """
    at = _at(held, node)
    if at is not None:
        return at
    ops = _linear(node, leaves, shared, lifted, held)
    held.append(node)
    lifted.append(ops)
    return len(lifted) - 1


def _shared(expr) -> list:
    """Nodes this expression reaches more than once, so each runs once.

    Reached BY VALUE: two structurally equal subtrees compute the same number
    over the same leaves, so folding them together is exact.
    """
    return [node for node, times in _reached(expr, []) if times > 1]


def _reached(expr, out: list) -> list:
    """``[node, times reached]`` for every node, a repeat counted but not walked."""
    if not isinstance(expr, Node):
        return out
    for seen in out:
        if seen[0] == expr:
            seen[1] += 1
            return out
    out.append([expr, 1])
    for arg in expr.args:
        _reached(arg, out)
    return out


def _at(items: list, node):
    """Where `node` sits in `items`, by value, or None."""
    for at, held in enumerate(items):
        if held == node:
            return at
    return None


def fold_shape(expr):
    """The one row shape this expression folds, or None if it folds none.

    Raises :class:`LangError` when two folds name different rows: one program
    steps ONE row shape, so it could only fold one of them correctly.
    """
    shapes = _fold_shapes(expr, [])
    if not shapes:
        return None
    if any(s != shapes[0] for s in shapes[1:]):
        raise LangError(
            f"this expression folds rows of {sorted(map(str, set(shapes)))}; a "
            f"band steps one ROW at a time when it reduces, so one program walks "
            f"one row shape. Write the reductions in separate statements"
        )
    return shapes[0]


def _fold_shapes(expr, out: list) -> list:
    """Every row shape a reduction inside this expression folds."""
    if not isinstance(expr, Node):
        return out
    if isinstance(expr, Fold):
        out.append((expr.rows, expr.cols))
    for arg in expr.args:
        _fold_shapes(arg, out)
    return out
