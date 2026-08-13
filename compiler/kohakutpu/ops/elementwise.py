"""One pass, one operator, at any rank. The unary and binary half of `ops/`.

`maximum`, `minimum` and `where` are here because a tinygrad graph cannot lower
without them: `MAX` is its clamp and `WHERE` is every mask it builds.
"""

from kohakuaccel.lang import units
from kohakutpu.lang import kernel

from kohakutpu import lang as L


@kernel
def neg(x=L.In(...), y=L.Out(...), *, part=8192):
    """``-x``, at any rank."""
    with units(x.parts(part)) as e:
        y[e] <<= -x[e]


@kernel
def absolute(x=L.In(...), y=L.Out(...), *, part=8192):
    """``|x|``, at any rank."""
    with units(x.parts(part)) as e:
        y[e] <<= L.absolute(x[e])


@kernel
def exp2(x=L.In(...), y=L.Out(...), *, part=8192):
    """``2**x``, at any rank."""
    with units(x.parts(part)) as e:
        y[e] <<= L.exp2(x[e])


@kernel
def log2(x=L.In(...), y=L.Out(...), *, part=8192):
    """``log2(x)``, at any rank."""
    with units(x.parts(part)) as e:
        y[e] <<= L.log2(x[e])


@kernel
def recip(x=L.In(...), y=L.Out(...), *, part=8192):
    """``1 / x``, at any rank."""
    with units(x.parts(part)) as e:
        y[e] <<= L.recip(x[e])


@kernel
def rsqrt(x=L.In(...), y=L.Out(...), *, part=8192):
    """``1 / sqrt(x)``, at any rank."""
    with units(x.parts(part)) as e:
        y[e] <<= L.rsqrt(x[e])


@kernel
def sub(a=L.In(...), b=L.In(...), y=L.Out(...), *, part=8192):
    """``a - b``, at any rank."""
    with units(a.parts(part)) as e:
        y[e] <<= a[e] - b[e]


@kernel
def mul(a=L.In(...), b=L.In(...), y=L.Out(...), *, part=8192):
    """``a * b``, at any rank."""
    with units(a.parts(part)) as e:
        y[e] <<= a[e] * b[e]


@kernel
def div(a=L.In(...), b=L.In(...), y=L.Out(...), *, part=8192):
    """``a / b``, at any rank."""
    with units(a.parts(part)) as e:
        y[e] <<= a[e] / b[e]


@kernel
def residual(a=L.In(...), b=L.In(...), y=L.Out(...), *, part=8192):
    """``a + b``, at any rank. Also tinygrad's bias path."""
    with units(a.parts(part)) as e:
        y[e] <<= a[e] + b[e]


@kernel
def maximum(a=L.In(...), b=L.In(...), y=L.Out(...), *, part=8192):
    """``max(a, b)``, at any rank. ONE word -- `vec_alu.v:144`."""
    with units(a.parts(part)) as e:
        y[e] <<= L.maximum(a[e], b[e])


@kernel
def minimum(a=L.In(...), b=L.In(...), y=L.Out(...), *, part=8192):
    """``min(a, b)``, at any rank. ONE word -- `vec_alu.v:145`."""
    with units(a.parts(part)) as e:
        y[e] <<= L.minimum(a[e], b[e])


@kernel
def where(c=L.In(...), a=L.In(...), b=L.In(...), y=L.Out(...), *, part=8192):
    """``c != 0 ? a : b``, at any rank, from a VALUE condition.

    `VSEL` reads its predicate from an ordinary vector register, so this is one
    word. A comparison is a different path -- it writes P0..P3 and no vector
    register -- and reaches the machine only fused to the select consuming it.
    """
    with units(c.parts(part)) as e:
        y[e] <<= L.where(c[e], a[e], b[e])
