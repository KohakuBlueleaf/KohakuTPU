"""E8M15, the vector core's lane format: FP32's exponent with a 15-bit
mantissa (a 16-bit significand), no subnormals.

A lane result is rounded to nearest even; a magnitude below the smallest
normal, 2^-126, flushes to zero; one at or above 2^128 is infinite. An `f32`
in memory is an E8M15 value with its mantissa zero-extended, so it holds the
same values.
"""

import numpy as np

#: Significand bits, the implicit one included.
SIGNIFICAND = 16
TINY = 2.0**-126
HUGE = 2.0**128


def value(x):
    """`x` (float64) rounded to E8M15's values, as float64."""
    x = np.asarray(x, np.float64)
    m, e = np.frexp(x)
    out = np.ldexp(np.round(np.ldexp(m, SIGNIFICAND)), e - SIGNIFICAND)
    out = np.where(np.abs(out) < TINY, 0.0, out)
    return np.where(np.abs(out) >= HUGE, np.copysign(np.inf, out), out)


__all__ = ["HUGE", "SIGNIFICAND", "TINY", "value"]
