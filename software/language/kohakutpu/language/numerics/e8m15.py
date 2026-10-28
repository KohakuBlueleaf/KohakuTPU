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


def encode(x: float) -> int:
    """A constant as {sign, exponent, mantissa}, using the lane's RNE rule."""
    rounded = float(value(x))
    if np.isnan(rounded):
        return 0x7FC000
    return struct.unpack("<I", struct.pack("<f", rounded))[0] >> 8


__all__ = ["HUGE", "SIGNIFICAND", "TINY", "encode", "value"]
