"""E8M15, the vector core's lane format: FP32's exponent with a 15-bit
mantissa (a 16-bit significand), no subnormals.

Lane results round to nearest even except in the effective-subtraction boundary
corner: `s >= 33`, residue leaving the 48-bit alignment window, and a
boundary-adjacent bit pattern can make the FMA and `mx_fpacc` split path read
exactly one ulp high, never low (see vector-core.md). The lane contract is
within one ulp; bit-exact tests exclude that corner. A magnitude below 2^-126
flushes to zero; one at or above 2^128 is infinite. An `f32` in memory has the
E8M15 mantissa zero-extended and holds the same values.
"""

import struct

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
    out = np.where(np.abs(x) < TINY, np.copysign(0.0, x), out)
    return np.where(np.abs(out) >= HUGE, np.copysign(np.inf, out), out)


def fma(a, b, c):
    """The lane FMA `a * b + c` (vadd is `a * 1 + c`, vsub `a * 1 + (-c)`,
    vmul `a * b + (+0)`, vfnma `(-a) * b + c`). A zero factor times a finite
    one is a zero product, and then the addend passes unchanged, its sign
    included (NUM.E8.008-.009): +0 + -0 is -0 when the -0 is the addend."""
    a, b, c = (np.asarray(t, np.float64) for t in (a, b, c))
    with np.errstate(all="ignore"):
        zero = ((a == 0) & np.isfinite(b)) | ((b == 0) & np.isfinite(a))
        return np.where(zero, c, value(a * b + c))


def moved(x):
    """What vmov, vsel, vmax, vmin, vneg and vabs return for their chosen
    (negated, cleared) operand `x`: `x * 1.0 + (+0)`, so a zero is +0
    (ISA.VC.058)."""
    x = np.asarray(x, np.float64)
    return np.where(x == 0, 0.0, x)


def encode(x: float) -> int:
    """A constant as {sign, exponent, mantissa}, using the lane's RNE rule."""
    rounded = float(value(x))
    if np.isnan(rounded):
        return 0x7FC000
    return struct.unpack("<I", struct.pack("<f", rounded))[0] >> 8


__all__ = ["HUGE", "SIGNIFICAND", "TINY", "encode", "fma", "moved", "value"]
