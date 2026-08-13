"""The L4 kernels a generated chain runs as: one per operand count.

A recipe rides as a KNOB, so `Kernel.trace` caches one structure per distinct
expression and the DSL below is untouched -- these are ordinary kernels that
happen to have their body written by :mod:`ktpugrad.chain` rather than by hand.

Three of each because a chain fills at most three DRAM operands: two descriptors
each, plus one shared store and one drain, against the eight a core has.

TWO families, because a fold needs the row width and an elementwise pass does
not: a `chain*` reads `...` and walks lanes, a `fold*` declares `(..., M, N)` so
the row travels with the expression. Kept apart so an elementwise expression
still runs the bytes it already runs.
"""

import operator

from kohakuaccel.lang import dims, units
from kohakutpu.hw.ops import OpKind
from kohakutpu.lang import kernel

from kohakutpu import lang as L
from ktpugrad.chain import Load, Op, Scalar
from ktpugrad.errors import ChainRefused

#: Our operator for each kind the reader emits. Written out rather than
#: composed: a kind with no entry is a refusal, never a guess.
BUILD = {
    OpKind.ADD: operator.add,
    OpKind.SUB: operator.sub,
    OpKind.MUL: operator.mul,
    OpKind.DIV: operator.truediv,
    OpKind.NEG: operator.neg,
    OpKind.ABS: L.absolute,
    OpKind.RECIP: L.recip,
    OpKind.RSQRT: L.rsqrt,
    OpKind.EXP2: L.exp2,
    OpKind.LOG2: L.log2,
    OpKind.SUM: L.row_sum,
    OpKind.RMAX: L.row_max,
}

#: What a kernel traces when nobody names an expression. A knob needs a default
#: and every real call passes one.
IDENTITY = Load(0)

#: The row a `fold*` walks. Named here so every one of them solves the same two
#: extents from the operand it is handed.
M, N = dims("M, N")

#: Elements one instance covers. A band steps one ROW when it reduces, so an
#: instance holding PART of the array refuses; every call passes the whole one.
WHOLE = 8192


def build(expr, values):
    """`expr` as a vector value over `values`, in this machine's own operators.

    `values` are the operands in recipe order, already the part this instance
    walks. Returns a `Value`, or a plain float for an expression over constants
    alone. Raises :class:`ChainRefused` for a kind no operator here spells.
    """
    if isinstance(expr, Load):
        return values[expr.at]
    if isinstance(expr, Scalar):
        return expr.value
    if not isinstance(expr, Op) or expr.kind not in BUILD:
        raise ChainRefused(f"{expr!r} is not an operation this generator spells")
    return BUILD[expr.kind](*(build(a, values) for a in expr.args))


@kernel
def chain1(a=L.In(...), y=L.Out(...), *, expr=IDENTITY):
    """One generated elementwise chain over one operand, at any rank."""
    y <<= build(expr, (a.whole(),))


@kernel
def chain2(a=L.In(...), b=L.In(...), y=L.Out(...), *, expr=IDENTITY):
    """One generated elementwise chain over two operands, at any rank."""
    y <<= build(expr, (a.whole(), b.whole()))


@kernel
def chain3(a=L.In(...), b=L.In(...), c=L.In(...), y=L.Out(...), *, expr=IDENTITY):
    """One generated elementwise chain over three operands, at any rank."""
    y <<= build(expr, (a.whole(), b.whole(), c.whole()))


@kernel
def fold1(a=L.In(..., M, N), y=L.Out(..., M, N), *, expr=IDENTITY, part=WHOLE):
    """One generated chain over one `[M][N]` operand, reducing along the row."""
    with units(a.parts(part)) as e:
        y[e] <<= build(expr, (a[e],))


@kernel
def fold2(
    a=L.In(..., M, N),
    b=L.In(..., M, N),
    y=L.Out(..., M, N),
    *,
    expr=IDENTITY,
    part=WHOLE,
):
    """One generated chain over two `[M][N]` operands, reducing along the row."""
    with units(a.parts(part)) as e:
        y[e] <<= build(expr, (a[e], b[e]))


@kernel
def fold3(
    a=L.In(..., M, N),
    b=L.In(..., M, N),
    c=L.In(..., M, N),
    y=L.Out(..., M, N),
    *,
    expr=IDENTITY,
    part=WHOLE,
):
    """One generated chain over three `[M][N]` operands, reducing along the row."""
    with units(a.parts(part)) as e:
        y[e] <<= build(expr, (a[e], b[e], c[e]))


#: Operand count to the kernel that takes it.
KERNELS = {1: chain1, 2: chain2, 3: chain3}

#: The same, for a chain that folds a row.
FOLDS = {1: fold1, 2: fold2, 3: fold3}
