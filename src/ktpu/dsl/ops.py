"""The free-function surface: everything that has no Python operator.

Deliberately close to the level-1 op set (docs/driver/ir.md s2.1), because a DSL
whose operations do not correspond to IR ops has to invent lowerings, and
lowerings are where semantics get lost. The exceptions are marked: `exp` and
`log` expand to `exp2`/`log2` with the constant folded, per the base-2 argument
in docs/compute/vector-core.md s4.1, and leave no trace of themselves.

**Some builtins already are DSL ops.** `abs(x)` reaches `Tracer.__abs__` and
emits `abs`; `-x` emits `neg`. `max(a, b)` deliberately does NOT work: the
builtin compares and branches, so it hits the `__bool__` guard and says so. That
is the right answer -- `maximum` is the op, and `max` is a branch.
"""

import math

from ktpu.dsl.errors import DSLError, at_caller
from ktpu.dsl.tracer import Tracer, binary, ternary, unary
from ktpu.ir import DType, OpKind

#: `exp` and `log` are base-2 ops with these folded in; see the module docstring.
LOG2_E = math.log2(math.e)
LN_2 = math.log(2.0)


# ---- elementwise, one operand ---------------------------------------------


def neg(x) -> Tracer:
    return unary(OpKind.NEG, x)


def recip(x) -> Tracer:
    """`1/x`. Also what `1.0 / x` emits, and a single pass on the vector core."""
    return unary(OpKind.RECIP, x)


def sqrt(x) -> Tracer:
    return unary(OpKind.SQRT, x)


def rsqrt(x) -> Tracer:
    """`1/sqrt(x)` in one op -- normalisation's whole inner loop."""
    return unary(OpKind.RSQRT, x)


def exp2(x) -> Tracer:
    return unary(OpKind.EXP2, x)


def log2(x) -> Tracer:
    return unary(OpKind.LOG2, x)


def exp(x) -> Tracer:
    """Sugar: `2^(x * log2 e)`. The constant folds; the graph holds only exp2."""
    return exp2(x * LOG2_E)


def log(x) -> Tracer:
    """Sugar: `log2(x) * ln 2`."""
    return log2(x) * LN_2


def relu(x) -> Tracer:
    return unary(OpKind.RELU, x)


# ---- elementwise, two and three operands ----------------------------------


def maximum(a, b) -> Tracer:
    """Elementwise max. The name is `maximum` because `max` is a branch."""
    return binary(OpKind.MAX, a, b)


def minimum(a, b) -> Tracer:
    return binary(OpKind.MIN, a, b)


def fma(a, b, c) -> Tracer:
    """`a * b + c` as one op, so the rounding is the hardware's single rounding."""
    return ternary(OpKind.FMA, a, b, c)


def where(cond, on_true, on_false) -> Tracer:
    """Data-dependent selection as a VALUE, which is the only form there is.

    Both sides are evaluated -- that is the trade every array language makes, and
    on a 16-lane SIMD engine it is the right one, since a branch would have to be
    predicated anyway (docs/isa/vector.md s2).

    `cond` is a mask: 1.0 or 0.0 in the operand dtype, as the comparisons
    produce. The operand order `(cond, on_true, on_false)` is pinned HERE -- the
    IR does not state one for `select`, so this is the convention level 2 has to
    match.
    """
    return ternary(OpKind.SELECT, cond, on_true, on_false)


# ---- contraction and conversion -------------------------------------------


def dot(a, b) -> Tracer:
    """`a @ b` on the last two axes, batch dims broadcast. M x K by K x N.

    The only macro-op. Whether it runs on a matmul cluster or in the vector
    core's tree mode is an engine-assignment decision at level 2, not something
    the surface distinguishes (docs/driver/dsl.md s7).
    """
    if not isinstance(a, Tracer):
        raise DSLError(
            f"dot needs traced values, got {type(a).__name__} first, at "
            f"{at_caller()}"
        )
    return a @ b


def cast(x, dtype: DType) -> Tracer:
    """The only way across dtypes. `x.to(dtype)` is the same op."""
    if not isinstance(x, Tracer):
        raise DSLError(
            f"cast needs a traced value, got {type(x).__name__}, at {at_caller()}"
        )
    return x.to(dtype)
