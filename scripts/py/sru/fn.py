"""The SRU function lane (S3), as specified, and the tables it runs.

    y = (-1)^sg * (c0 + c1 * t) * 2^(es + n)

The input x (E8M15) is turned into a fixed-point v = x * 2^pre with 24
fraction bits (magnitude truncated, clamped to 2^33 - 1), then:

    EXP  w = v - (floor(k) << 24), floor(k) clamped to [-512, 511];
         n = floor(w / 2^24); the fraction picks segment and t.
    FIX  n = 0; v in [-16, 16) picks segment and t over 1,024 segments of
         1/32; below the range is segment 0 at t = 0, above it the last
         segment at t = max.

A table entry is 36 bits: [35] sg, [34:29] es (signed), [28:13] c0's 16
fraction bits (c0 = 1.f, implicit one), [12:0] c1 (signed, unit 2^(cs-15)).
es = -32 makes the segment a signed zero. cs (0..2) is one per table: steep
tails (phi) need a coarser slope unit. With t a 14-bit fraction,
Y = (c0 << 13) + c1 * (t << cs) is exact in units of 2^-29, lies in
[2^28, 2^30 + 2^28), and is rounded once, to nearest even on 16 bits.

    MANT x = m * 2^e (m in [1, 2), e unbiased) picks the segment from m:
         n = e, or -e with NEG; the result's sign is x's times the entry's.
    HALF e = 2q + r: the segment from (r, m), n = q, or -q with NEG; a
         negative x is NaN.

A RAM table has 1,024 segments over the fraction (EXP) or [-16, 16) (FIX);
MANT takes m's top 10 bits (t its last 5), HALF r and m's top 9 (t its last
6). The ROM kind is exp2 alone with 128 segments. The mantissa ROM (the
finalise's RCP / RSQRT on pool lane 0) is 1/m (128 segments, MANT NEG) and
1/sqrt(m 2^r) (2 x 128, HALF NEG): index {HALF, r, m[14:8]}, t m[7:0].

MANT / HALF specials: NaN -> NaN; a zero is infinite with NEG, else zero;
an infinity is zero with NEG, else infinite; each keeps x's sign.
"""

import math
from fractions import Fraction

import e8

FW = 24
VMAX = (1 << 33) - 1


def fixed(x, pre):
    """v = trunc(x * 2^pre * 2^24), clamped; x a finite or infinite word."""
    s, e, m = e8.fields(x)
    if e == 255:
        mag = VMAX
    elif e == 0:
        mag = 0
    else:
        sh = e - 142 + pre + FW
        sig = 0x8000 + m
        mag = sig << sh if sh >= 0 else sig >> -sh
        mag = min(mag, VMAX)
    return -mag if s else mag


def floor_k(k):
    """floor(k) clamped to [-512, 511]; k finite or infinite."""
    s, e, _ = e8.fields(k)
    if e == 255:
        return -512 if s else 511
    v = e8.value(k)
    return max(-512, min(511, math.floor(v)))


#: Function modes (the op's FIX and MANT bits).
EXP, FIX, MANT, HALF = 0, 1, 2, 3


def mant_index(x, half, neg, mrom=False):
    """(segment, t, n) of a MANT / HALF input (finite, nonzero)."""
    _, e, m = e8.fields(x)
    eu = e - 127
    q, r = (eu >> 1, eu & 1) if half else (eu, 0)
    if mrom:
        seg, t = (half << 8) | (r << 7) | (m >> 8), (m & 0xFF) << 6
    elif half:
        seg, t = (r << 9) | (m >> 6), (m & 0x3F) << 8
    else:
        seg, t = m >> 5, (m & 0x1F) << 9
    return seg, t, -q if neg else q


def index(x, k, fix, pre, rom=False):
    """(segment, t, n) for one EXP / FIX input."""
    v = fixed(x, pre)
    if fix:
        lo, hi = -16 << FW, 16 << FW
        if v < lo:
            return 0, 0, 0
        if v >= hi:
            return 1023, 0x3FFF, 0
        u = v - lo
        return u >> 19, (u >> 5) & 0x3FFF, 0
    w = v - (floor_k(k) << FW)
    n = w >> FW
    f = w & ((1 << FW) - 1)
    if rom:
        return f >> 17, (f >> 3) & 0x3FFF, n
    return f >> 14, f & 0x3FFF, n


def lane(x, k, mode, pre, table, rom=False, neg=0, mrom=None):
    """The lane's output word for one input; table = (entries, cs); mode
    EXP / FIX / MANT / HALF; mrom: the mantissa ROM's (entries, cs), used in
    place of the table."""
    if mode >= MANT:
        return _mant(x, mode == HALF, neg, mrom or table, mrom is not None)
    if e8.is_nan(x) or (not mode and e8.is_nan(k)):
        return e8.NAN
    seg, t, n = index(x, k, mode, pre, rom)
    return _seg(table, seg, t, n, 0)


def _mant(x, half, neg, table, mrom):
    s, e, m = e8.fields(x)
    if e8.is_nan(x) or (half and s and e != 0):
        return e8.NAN
    if e == 0 or e == 255:
        inf = (e == 0) == bool(neg)
        return (s << 23) | ((0xFF << 15) if inf else 0)
    seg, t, n = mant_index(x, half, neg, mrom)
    return _seg(table, seg, t, n, 0 if half else s)


def _seg(table, seg, t, n, flip):
    """Entry `seg` evaluated at t, times 2^n, its sign flipped by `flip`."""
    entries, cs = table
    t <<= cs
    ent = entries[seg]
    sg = (ent >> 35) ^ flip
    es = (ent >> 29) & 0x3F
    es = es - 64 if es & 0x20 else es
    c0 = 0x10000 | ((ent >> 13) & 0xFFFF)
    c1 = ent & 0x1FFF
    c1 = c1 - 0x2000 if c1 & 0x1000 else c1
    if es == ZERO_ES:
        return sg << 23
    y = (c0 << 13) + c1 * t
    assert y > 0
    return e8.encode(Fraction(-y if sg else y, 1 << 29) * Fraction(2) ** (es + n), sg)


def entry(sg, es, c0, c1, cs):
    """Pack an entry; c0 in [1, 2) and c1 as reals, c1 in units of 2^(cs-15)."""
    q0 = round((c0 - 1) * (1 << 16))
    q1 = round(c1 * (1 << (15 - cs)))
    if q0 == 1 << 16:
        q0 = (1 << 16) - 1
    assert 0 <= q0 < 1 << 16, c0
    assert -(1 << 12) <= q1 < 1 << 12, c1
    assert -32 <= es < 32, es
    return (sg << 35) | ((es & 0x3F) << 29) | (q0 << 13) | (q1 & 0x1FFF)


ZERO_ES = -32


def segment(f, a, b):
    """Best line for f over [a, b] in normalised form: (sg, es, c0, c1)."""
    fa, fb, fm = f(a), f(b), f((a + b) / 2)
    sg = 1 if fa < 0 or fb < 0 else 0
    ma, mb, mm = abs(fa), abs(fb), abs(fm)
    if min(ma, mb) < 2.0**-30:
        return sg, ZERO_ES, 1.0, 0.0
    es = math.floor(math.log2(ma))
    # Chord minus half its midpoint deviation: equal error at ends and middle.
    d = (ma + mb) / 2 - mm
    c0 = (ma - d / 2) / 2.0**es
    c1 = (mb - ma) / 2.0**es
    if c0 < 1:
        es -= 1
        c0, c1 = c0 * 2, c1 * 2
    elif c0 >= 2:
        es += 1
        c0, c1 = c0 / 2, c1 / 2
    return sg, es, c0, c1


def pack(segs):
    """(entries, cs): the smallest slope scale cs in 0..2 every segment fits."""
    top = max(abs(s[3]) for s in segs)
    cs = 0
    while cs < 2 and top * (1 << (15 - cs)) >= (1 << 12) - 0.5:
        cs += 1
    return [entry(*s, cs) for s in segs], cs


def exp2_table(nseg):
    return pack(
        [segment(lambda u: 2.0**u, i / nseg, (i + 1) / nseg) for i in range(nseg)]
    )


def fix_table(f):
    return pack([segment(f, -16 + i / 32, -16 + (i + 1) / 32) for i in range(1024)])


def sigmoid(u):
    return 1.0 / (1.0 + math.exp(-u))


def phi(u):
    return 0.5 * math.erfc(-u / math.sqrt(2.0))


def mant_segs(f, nseg):
    """f over m in [1, 2) in nseg segments."""
    return [segment(f, 1 + i / nseg, 1 + (i + 1) / nseg) for i in range(nseg)]


def half_segs(f, nseg):
    """r = 0: f(m), r = 1: f(2m), m in [1, 2), nseg segments each."""
    return mant_segs(f, nseg) + mant_segs(lambda u: f(2 * u), nseg)


def mrom_table():
    """The mantissa ROM: 1/m at 0..127, 1/sqrt(m 2^r) at 256..511."""
    rsq = lambda u: 1 / math.sqrt(u)  # noqa: E731
    segs = mant_segs(lambda u: 1 / u, 128) + [(0, ZERO_ES, 1.0, 0.0)] * 128
    return pack(segs + half_segs(rsq, 128))


TABLES = {
    "exp2": lambda: exp2_table(1024),
    "sigmoid": lambda: fix_table(sigmoid),
    # Phi is flat outside [-8, 8): run it at pre = 1, segments of 1/64.
    "phi": lambda: fix_table(lambda u: phi(u / 2)),
    "recip": lambda: pack(mant_segs(lambda u: 1 / u, 1024)),
    "rsqrt": lambda: pack(half_segs(lambda u: 1 / math.sqrt(u), 512)),
    "sqrt": lambda: pack(half_segs(math.sqrt, 512)),
}

# The pre a table is built for.
PRE = {"exp2": 0, "sigmoid": 0, "phi": 1, "recip": 0, "rsqrt": 0, "sqrt": 0}
#: The mode and NEG a table runs at.
MODE = {
    "exp2": (EXP, 0),
    "sigmoid": (FIX, 0),
    "phi": (FIX, 0),
    "recip": (MANT, 1),
    "rsqrt": (HALF, 1),
    "sqrt": (HALF, 0),
}
