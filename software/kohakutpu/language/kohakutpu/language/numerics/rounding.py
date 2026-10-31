"""Lane-instruction rounding patterns parameterised by vc.sig_bits."""


def whole(sig_bits: int) -> float:
    """The first binade whose lane values are all integers."""
    return 2.0 ** (sig_bits - 1)


def trunc(x, arith, sig_bits: int):
    """Toward zero across the fractional range, with an offset of x's sign."""
    limit = whole(sig_bits)
    neg = arith.math("cmplt", x, 0.0)
    offset = arith.where(neg, -limit, limit)
    r = (x + offset) - offset
    floor = r - arith.math("cmpgt", r, x)
    ceil = r + arith.math("cmplt", r, x)
    small = arith.where(neg, ceil, floor)
    return arith.where(arith.math("cmplt", arith.absolute(x), limit), small, x)


__all__ = ["trunc", "whole"]
