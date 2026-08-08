"""Kernels written in the DSL, which is how the surface gets tested.

docs/driver/dsl.md s6 step 5 calls this the honest test of the whole design: if
a kernel from docs/compute/vector-bringup.md s3.2 needs an escape hatch, the
surface is wrong. None of these does. Every one is ordinary Python arithmetic on
tracers, and what each leaves behind is a graph of level-1 ops with no
`softmax`, `layernorm` or `gelu` node anywhere in it -- because those are
compositions, and emitting them as single nodes would hide the fusion that is
this machine's entire performance story (docs/driver/ir.md s2.1).

These are plain functions, not `@kernel`s. A `Kernel` is an entry point and does
not compose; a function inlines into whatever is tracing it, which is the point.
"""

from ktpu.dsl.ops import exp, recip, rsqrt
from ktpu.dsl.tracer import Tracer
from ktpu.ir import E8M15, FP16, DType

#: gelu's tanh approximation: sqrt(2/pi) and the cubic's coefficient.
GELU_SCALE = 0.7978845608028654
GELU_CUBIC = 0.044715

#: gelu's cheaper sigmoid approximation, which the literature fits at 1.702.
GELU_SIGMOID_SCALE = 1.702


def sigmoid(x: Tracer) -> Tracer:
    """`1 / (1 + e^-x)`. Four ops: neg, exp2, add, recip."""
    return recip(1.0 + exp(-x))


def tanh(x: Tracer) -> Tracer:
    """`1 - 2/(e^2x + 1)`.

    Written this way rather than `(e^x - e^-x)/(e^x + e^-x)` because it needs one
    exponential instead of two, and because the saturating limits come out right
    without a guard: as x grows the exponential overflows to infinity and the
    reciprocal to zero, giving +1, and as it falls the exponential underflows to
    zero, giving -1.
    """
    return 1.0 - 2.0 * recip(exp(2.0 * x) + 1.0)


def silu(x: Tracer) -> Tracer:
    """`x * sigmoid(x)`, also called swish."""
    return x * sigmoid(x)


def gelu(x: Tracer, approximate: str = "tanh") -> Tracer:
    """Gaussian error linear unit.

    Approximated, because the exact form needs `erf` and the level-1 op set has
    no `erf` and should not grow one -- it is not an op any hardware here
    executes. The tanh form is what every framework ships as `approximate=tanh`
    and agrees with the exact function to about 1e-3 absolute.

    `approximate` is an ordinary Python string, so this `match` runs at trace
    time and only the chosen expansion reaches the graph. That is compile-time
    control flow doing exactly what docs/driver/dsl.md s2 says it does.
    """
    match approximate:
        case "tanh":
            inner = GELU_SCALE * (x + GELU_CUBIC * (x * x * x))
            return 0.5 * x * (1.0 + tanh(inner))
        case "sigmoid":
            return x * sigmoid(GELU_SIGMOID_SCALE * x)
        case _:
            raise ValueError(
                f"gelu approximate must be 'tanh' or 'sigmoid', " f"got {approximate!r}"
            )


def rmsnorm(x: Tracer, g: Tracer, eps: float = 1e-6, axis: int = -1) -> Tracer:
    """`x * rsqrt(mean(x^2) + eps) * g`.

    `mean` is sugar and leaves nothing of itself behind, so the graph is
    mul, sum, mul, add, rsqrt, mul, mul. The leading `mul` + `sum` is what the
    `sumsq` op exists to be -- recognising the pair is a peephole at level 2,
    which is where the fusion decisions live, and writing it as `x.sumsq()` here
    would hide the composition instead.
    """
    ms = (x * x).mean(axis=axis, keepdim=True)
    return x * rsqrt(ms + eps) * g


def layernorm(
    x: Tracer, g: Tracer, b: Tracer, eps: float = 1e-5, axis: int = -1
) -> Tracer:
    """Mean-centre, scale by the reciprocal standard deviation, affine.

    The deviation is computed once and reused, which `x.var()` cannot do -- it
    has to take its own mean. Two reductions either way; this saves a pass over
    the data, and on a machine that is memory-bound below 2 ops per element
    (docs/compute/vector-bringup.md s2.1) a pass is the unit that matters.
    """
    deviation = x - x.mean(axis=axis, keepdim=True)
    variance = (deviation * deviation).mean(axis=axis, keepdim=True)
    return deviation * rsqrt(variance + eps) * g + b


def softmax(x: Tracer, axis: int = -1) -> Tracer:
    """Max-subtract, exponentiate, normalise -- the numerically safe form.

    Subtracting the row max is not optional here. `exp` is `exp2` with the
    constant folded, and E8M15's largest exponent is 127, so an unshifted
    `exp2(x * log2 e)` overflows at x = 88 -- reachable on real logits.
    """
    shifted = x - x.max(axis=axis, keepdim=True)
    weights = exp(shifted)
    return weights / weights.sum(axis=axis, keepdim=True)


def split_k_epilogue(partials: Tracer, out_dtype: DType = FP16) -> Tracer:
    """Sum S matmul partials, and convert exactly once.

    `partials` is `(S, ..., M, N)` in ACC24: S clusters each swept K/S and
    emitted without converting. The cast to E8M15 is range-lossless -- E7's
    exponent range is strictly inside E8's -- and costs one rounding of the
    bottom mantissa bit, 2^-17 relative. The sum then carries FP32's exponent
    range, so the ceiling is 3.4e38 rather than FP16's 65,504, and only the final
    store can saturate. docs/compute/vector-core.md s11.3.

    That last conversion is the one an FP16 output makes silently, so a caller
    that needs the range says `out_dtype=FP32`.
    """
    return partials.to(E8M15).sum(axis=0).to(out_dtype)
