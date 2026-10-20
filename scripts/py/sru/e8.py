"""E8M15 reference arithmetic for the SRU benches, in exact rationals.

A word is 24 bits {s, e[7:0], m[14:0]}: e = 0 is a signed zero, e = 255 is an
infinity (m = 0) or a NaN; otherwise the value is (2^15 + m) * 2^(e - 142).
Every result is rounded once, to nearest even on a 16-bit significand, with an
unbounded exponent; then a field <= 0 is a signed zero and >= 255 a signed
infinity. NaN is 0x7FC000.

This file is written from the format definition, never from the RTL.
"""

from fractions import Fraction

NAN = 0x7FC000


def fields(w):
    return (w >> 23) & 1, (w >> 15) & 0xFF, w & 0x7FFF


def is_nan(w):
    _, e, m = fields(w)
    return e == 255 and m != 0


def is_inf(w):
    _, e, m = fields(w)
    return e == 255 and m == 0


def is_zero(w):
    return fields(w)[1] == 0


def value(w):
    """Exact value of a finite word."""
    s, e, m = fields(w)
    if e == 0:
        return Fraction(0)
    v = Fraction(0x8000 + m) * Fraction(2) ** (e - 142)
    return -v if s else v


def encode(x, zero_sign=0):
    """Round an exact rational to E8M15."""
    if x == 0:
        return zero_sign << 23
    s = 1 if x < 0 else 0
    a = -x if s else x

    # E with 2^E <= a < 2^(E+1).
    big = a.numerator.bit_length() - a.denominator.bit_length()
    if Fraction(2) ** big > a:
        big -= 1
    scaled = a / Fraction(2) ** (big - 15)  # in [2^15, 2^16)

    q, r = divmod(scaled.numerator, scaled.denominator)
    half = 2 * r - scaled.denominator
    if half > 0 or (half == 0 and q & 1):
        q += 1
    if q == 0x10000:
        q >>= 1
        big += 1

    e = big + 127
    if e >= 255:
        return (s << 23) | (0xFF << 15)
    if e <= 0:
        return s << 23
    return (s << 23) | (e << 15) | (q - 0x8000)


def fma(a, b, c, add=True):
    """a*b + c (add=False: a*b), one rounding."""
    sa, sb = fields(a)[0], fields(b)[0]
    sp = sa ^ sb
    sc = fields(c)[0] if add else sp

    p_nan = (
        is_nan(a)
        or is_nan(b)
        or (is_inf(a) and is_zero(b))
        or (is_zero(a) and is_inf(b))
    )
    p_inf = (is_inf(a) or is_inf(b)) and not p_nan
    c_nan = add and is_nan(c)
    c_inf = add and is_inf(c)
    if p_nan or c_nan or (p_inf and c_inf and sp != sc):
        return NAN
    if p_inf:
        return (sp << 23) | (0xFF << 15)
    if c_inf:
        return (sc << 23) | (0xFF << 15)

    exact = value(a) * value(b)
    if add:
        exact += value(c)
    both_zero = (is_zero(a) or is_zero(b)) and (not add or is_zero(c))
    return encode(exact, (sp & sc) if both_zero else 0)
