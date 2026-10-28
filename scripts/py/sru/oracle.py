"""Independent mathematical and number-format oracles for the SRU benches.

Derived strictly from the number-format and ISA specifications in:
- docs/spec/stack/numerics/formats.md (NUM.E8.001, NUM.E8.006, NUM.F16.001-005,
  NUM.MX7.001-007, NUM.ACC.004-005, NUM.GT4.001)
- docs/spec/stack/isa/sru.md (ISA.SR.004, ISA.SR.010-015, ISA.SR.020-023,
  ISA.SR.030-031, ISA.SR.040-047, ISA.SR.050-053)
- docs/spec/stack/rulings.md (RUL.056, RUL.057, RUL.061)
- docs/projects/kohakutpu/sru.md (sections 1, 3, 4, 5, 5a, 6, 7, 8)
- docs/projects/kohakutpu/number-format.md (sections 2, 3, 5)

No hardware behaviour model (core.py, red.py, fn.py, e8.py) is imported.
"""

import math
from fractions import Fraction

E8_NAN = 0x7FC000
E8_ONE = 0x3F8000
ZERO_ES = -32

# Function modes (ISA.SR.022, ISA.SR.043: 2 * MANT + FIX)
FN_EXP, FN_FIX, FN_MANT, FN_HALF = 0, 1, 2, 3

# Finalise opcodes (ISA.SR.023, ISA.SR.044)
FIN_MOV = 0
FIN_ADD = 3
FIN_SUB = 4
FIN_MUL = 5
FIN_FMA = 6
FIN_MAX = 8
FIN_MIN = 9
FIN_RCP = 10
FIN_RSQRT = 11

# MXFP7 constants and reciprocal table (NUM.MX7.001-003)
MX7_SBIAS = 20
MX7_KBLOCK = 32
MX7_QMAX = 63
MX7_RECIP = {
    8: 4096,
    9: 3641,
    10: 3277,
    11: 2979,
    12: 2731,
    13: 2520,
    14: 2341,
    15: 2185,
}


# ---------------------------------------------------------------------------
# E8M15 format & arithmetic (NUM.E8.001, NUM.E8.006, RUL.056, ISA.SR.043)
# ---------------------------------------------------------------------------


def e8_fields(w: int) -> tuple[int, int, int]:
    """Unpack 24-bit E8M15 word into (sign, biased_exp, mantissa_15)."""
    return (w >> 23) & 1, (w >> 15) & 0xFF, w & 0x7FFF


def e8_is_nan(w: int) -> bool:
    _, e, m = e8_fields(w)
    return e == 255 and m != 0


def e8_is_inf(w: int) -> bool:
    _, e, m = e8_fields(w)
    return e == 255 and m == 0


def e8_is_zero(w: int) -> bool:
    return e8_fields(w)[1] == 0


def e8_value(w: int) -> Fraction:
    """Exact rational value of a finite E8M15 word (NUM.E8.001)."""
    s, e, m = e8_fields(w)
    if e == 0:
        return Fraction(0)
    mag = Fraction(0x8000 + m) * (Fraction(2) ** (e - 142))
    return -mag if s else mag


def e8_encode(x: Fraction | float, zero_sign: int = 0) -> int:
    """Round an exact rational or float to E8M15 by RNE (NUM.E8.001, RUL.056)."""
    if isinstance(x, float):
        if math.isnan(x):
            return E8_NAN
        s_bit = 1 if math.copysign(1.0, x) < 0 else 0
        if math.isinf(x):
            return (s_bit << 23) | (0xFF << 15)
        if x == 0.0:
            return s_bit << 23
        x = Fraction(x)
    elif not isinstance(x, Fraction):
        x = Fraction(x)

    if x == 0:
        return (zero_sign & 1) << 23
    s = 1 if x < 0 else 0
    a = -x if s else x

    big = a.numerator.bit_length() - a.denominator.bit_length()
    if (Fraction(2) ** big) > a:
        big -= 1
    scaled = a / (Fraction(2) ** (big - 15))
    q, r = divmod(scaled.numerator, scaled.denominator)
    cmp_half = 2 * r - scaled.denominator
    if cmp_half > 0 or (cmp_half == 0 and (q & 1)):
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


def e8_fma(a: int, b: int, c: int, add: bool = True) -> int:
    """Single-rounding E8M15 fused multiply-add a * b + c (ISA.SR.043, NUM.E8.001)."""
    sa, sb = e8_fields(a)[0], e8_fields(b)[0]
    sp = sa ^ sb
    sc = e8_fields(c)[0] if add else sp

    p_nan = (
        e8_is_nan(a)
        or e8_is_nan(b)
        or (e8_is_inf(a) and e8_is_zero(b))
        or (e8_is_zero(a) and e8_is_inf(b))
    )
    p_inf = (e8_is_inf(a) or e8_is_inf(b)) and not p_nan
    c_nan = add and e8_is_nan(c)
    c_inf = add and e8_is_inf(c)
    if p_nan or c_nan or (p_inf and c_inf and sp != sc):
        return E8_NAN
    if p_inf:
        return (sp << 23) | (0xFF << 15)
    if c_inf:
        return (sc << 23) | (0xFF << 15)

    exact = e8_value(a) * e8_value(b)
    if add:
        exact += e8_value(c)
    both_zero = (e8_is_zero(a) or e8_is_zero(b)) and (not add or e8_is_zero(c))
    return e8_encode(exact, (sp & sc) if both_zero else 0)


# ---------------------------------------------------------------------------
# FP16 <-> E8M15 conversions (NUM.F16.001-005, ISA.SR.042, ISA.SR.047, RUL.057)
# ---------------------------------------------------------------------------


def f16_to_e8_ieee(h: int) -> int:
    """IEEE binary16 -> E8M15, exact (NUM.F16.001, NUM.F16.002, ISA.SR.047)."""
    s, e, m = (h >> 15) & 1, (h >> 10) & 0x1F, h & 0x3FF
    if e == 31:
        return (s << 23) | (0xFF << 15) | (m << 5)
    if e == 0:
        if m == 0:
            return s << 23
        v = Fraction(m, 1 << 24)
    else:
        v = Fraction(0x400 + m, 1 << 10) * (Fraction(2) ** (e - 15))
    return e8_encode(-v if s else v, s)


def f16_to_e8_acc(h: int) -> int:
    """Accumulator-form FP16 -> E8M15: e=0 signed zero, 1..31 binades (ISA.SR.047, RUL.057)."""
    s, e, m = (h >> 15) & 1, (h >> 10) & 0x1F, h & 0x3FF
    if e == 0:
        return s << 23
    return (s << 23) | ((e + 112) << 15) | (m << 5)


def e8_to_f16_acc(w: int) -> int:
    """E8M15 -> accumulator-form FP16: RNE, <2^-14 signed zero, >=2^16 +-65504 (ISA.SR.047)."""
    s, e, _ = e8_fields(w)
    if e == 0:
        return s << 15
    if e == 255:
        return (s << 15) | 0x7BFF
    a = abs(e8_value(w))
    big = a.numerator.bit_length() - a.denominator.bit_length()
    if (Fraction(2) ** big) > a:
        big -= 1
    q = a / (Fraction(2) ** (big - 10))
    n, r = divmod(q.numerator, q.denominator)
    if 2 * r > q.denominator or (2 * r == q.denominator and (n & 1)):
        n += 1
    if n == 2048:
        n, big = 1024, big + 1
    if big < -14:
        return s << 15
    if big >= 16:
        return (s << 15) | 0x7BFF
    return (s << 15) | ((big + 15) << 10) | (n - 1024)


def e8_to_f16_ieee(w: int) -> int:
    """E8M15 -> IEEE FP16 with subnormals and finite saturation at 65504 (NUM.F16.003-005)."""
    s, e, m = e8_fields(w)
    if e == 0:
        return s << 15
    if e == 255:
        return (s << 15) | (0x1F << 10) | ((0x200 | (m >> 5)) if m else 0)
    a = abs(e8_value(w))
    big = a.numerator.bit_length() - a.denominator.bit_length()
    if (Fraction(2) ** big) > a:
        big -= 1
    step = Fraction(2) ** (max(big, -14) - 10)
    q = a / step
    n, r = divmod(q.numerator, q.denominator)
    if 2 * r > q.denominator or (2 * r == q.denominator and (n & 1)):
        n += 1
    val = n * step
    if val >= Fraction(65520):
        return (s << 15) | 0x7BFF
    if val == 0:
        return s << 15
    big = val.numerator.bit_length() - val.denominator.bit_length()
    if (Fraction(2) ** big) > val:
        big -= 1
    if big < -14:
        return (s << 15) | int(val / (Fraction(2) ** -24))
    mant = int(val / (Fraction(2) ** (big - 10))) - 0x400
    return (s << 15) | ((big + 15) << 10) | mant


# ---------------------------------------------------------------------------
# MXFP7 quantiser and entry packer (NUM.MX7.001-005, NUM.MX7.007, ISA.SR.046)
# ---------------------------------------------------------------------------


def mx7_scale_field(peak_bits: int) -> tuple[int, int]:
    """Derive (es, m8) from a 32-element row's peak FP16 magnitude bits[14:0] (NUM.MX7.002)."""
    ep_f = (peak_bits >> 10) & 0x1F
    frac = peak_bits & 0x3FF
    if ep_f == 0 and frac == 0:
        return 0, 8
    sig_p = frac if ep_f == 0 else ((1 << 10) | frac)
    ep = 1 if ep_f == 0 else ep_f
    while sig_p < 1024:
        sig_p <<= 1
        ep -= 1
    m8 = (sig_p + 125) // 126
    if m8 >= 16:
        es = ep - 20
        m8 = 9 if m8 == 17 else 8
    else:
        es = ep - 21
    es = max(-MX7_SBIAS, min((1 << 5) - 1 - MX7_SBIAS, es))
    return es, m8


def mx7_quantise_row(row_f16: list[int]) -> tuple[list[int], int]:
    """Quantise one row of 32 FP16 bit patterns to 32 int7 values and an 8-bit E5M3 scale field
    (NUM.MX7.001-003)."""
    assert len(row_f16) == MX7_KBLOCK
    peak_bits = max(h & 0x7FFF for h in row_f16)
    es, m8 = mx7_scale_field(peak_bits)
    recip = MX7_RECIP[m8]
    field = (((es + MX7_SBIAS) & 0x1F) << 3) | (m8 - 8)
    qs = []
    for h in row_f16:
        sign = (h >> 15) & 1
        e_f = (h >> 10) & 0x1F
        frac = h & 0x3FF
        sig = frac if e_f == 0 else ((1 << 10) | frac)
        e = 1 if e_f == 0 else e_f
        prod = sig * recip
        sh = 25 + 12 + es - e
        if sh > 40 or sig == 0:
            mag = 0
        elif sh <= 0:
            mag = MX7_QMAX
        else:
            mag = (prod >> sh) + ((prod >> (sh - 1)) & 1)
            mag = min(mag, MX7_QMAX)
        qs.append(-mag if sign else mag)
    return qs, field


def mx7_pack_entry(rows_f16: list[list[int]], b_layout: bool) -> list[int]:
    """Quantise and pack one 4x32 FP16 entry into four 256-bit MXFP7 words (NUM.MX7.005)."""
    assert len(rows_f16) == 4
    q_rows = []
    fields = []
    for r in range(4):
        qs, fld = mx7_quantise_row(rows_f16[r])
        q_rows.append(qs)
        fields.append(fld)
    words = []
    for w in range(4):
        word = 0
        for slot in range(32):
            lane, kk = (slot % 4, slot // 4) if b_layout else (slot // 8, slot % 8)
            word |= (q_rows[lane][w * 8 + kk] & 0x7F) << (255 - slot * 7 - 6)
        for r in range(4):
            word |= fields[r] << (31 - r * 8 - 7)
        words.append(word)
    return words


def mx7_subrow_words(fp_words: list[list[int]], b_layout: bool) -> list[int]:
    """Pack a sub-row's FP16 sub-tile words (multiples of 8) into MXFP7 words
    (ISA.SR.040, ISA.SR.046, NUM.MX7.005, NUM.MX7.007)."""
    out = []
    for e in range(len(fp_words) // 8):
        rows_f16 = [[0] * 32 for _ in range(4)]
        for w in range(8):
            wd = fp_words[8 * e + w]
            for r in range(4):
                for c in range(4):
                    rows_f16[r][4 * w + c] = wd[4 * r + c]
        out.extend(mx7_pack_entry(rows_f16, b_layout))
    return out


# ---------------------------------------------------------------------------
# Function lane & piecewise-linear table specification (ISA.SR.043, sru.md s4)
# ---------------------------------------------------------------------------


def fn_entry_pack(sg: int, es: int, c0: float, c1: float, cs: int) -> int:
    """Pack a 36-bit function-table entry {sg, es[5:0], q0[15:0], q1[12:0]} (sru.md s4)."""
    q0 = round((c0 - 1.0) * (1 << 16))
    q1 = round(c1 * (1 << (15 - cs)))
    if q0 == 1 << 16:
        q0 = (1 << 16) - 1
    assert 0 <= q0 < (1 << 16)
    assert -(1 << 12) <= q1 < (1 << 12)
    assert -32 <= es < 32
    return ((sg & 1) << 35) | ((es & 0x3F) << 29) | (q0 << 13) | (q1 & 0x1FFF)


def fn_segment(f, a: float, b: float) -> tuple[int, int, float, float]:
    """Secant-midpoint linear segment for f over [a, b] in normalised form."""
    fa, fb, fm = f(a), f(b), f((a + b) / 2.0)
    sg = 1 if (fa < 0 or fb < 0) else 0
    ma, mb, mm = abs(fa), abs(fb), abs(fm)
    if min(ma, mb) < 2.0**-30:
        return sg, ZERO_ES, 1.0, 0.0
    es = math.floor(math.log2(ma))
    d = (ma + mb) / 2.0 - mm
    c0 = (ma - d / 2.0) / (2.0**es)
    c1 = (mb - ma) / (2.0**es)
    if c0 < 1.0:
        es -= 1
        c0, c1 = c0 * 2.0, c1 * 2.0
    elif c0 >= 2.0:
        es += 1
        c0, c1 = c0 / 2.0, c1 / 2.0
    return sg, es, c0, c1


def fn_pack_segments(
    segs: list[tuple[int, int, float, float]],
) -> tuple[list[int], int]:
    """Pack segment tuples into (entries, cs) with minimal slope scale cs in 0..2."""
    top = max(abs(s[3]) for s in segs)
    cs = 0
    while cs < 2 and top * (1 << (15 - cs)) >= (1 << 12) - 0.5:
        cs += 1
    return [fn_entry_pack(*s, cs) for s in segs], cs


def fn_exp2_table(nseg: int) -> tuple[list[int], int]:
    return fn_pack_segments(
        [fn_segment(lambda u: 2.0**u, i / nseg, (i + 1) / nseg) for i in range(nseg)]
    )


def fn_fix_table(f) -> tuple[list[int], int]:
    return fn_pack_segments(
        [fn_segment(f, -16.0 + i / 32.0, -16.0 + (i + 1) / 32.0) for i in range(1024)]
    )


def fn_mant_segs(f, nseg: int) -> list[tuple[int, int, float, float]]:
    return [fn_segment(f, 1.0 + i / nseg, 1.0 + (i + 1) / nseg) for i in range(nseg)]


def fn_half_segs(f, nseg: int) -> list[tuple[int, int, float, float]]:
    return fn_mant_segs(f, nseg) + fn_mant_segs(lambda u: f(2.0 * u), nseg)


def fn_mrom_table() -> tuple[list[int], int]:
    """The 512-entry mantissa ROM: 1/m at 0..127, 1/sqrt(m 2^r) at 256..511 (sru.md s4)."""
    segs = (
        fn_mant_segs(lambda u: 1.0 / u, 128)
        + [(0, ZERO_ES, 1.0, 0.0)] * 128
        + fn_half_segs(lambda u: 1.0 / math.sqrt(u), 128)
    )
    return fn_pack_segments(segs)


def fn_sigmoid(u: float) -> float:
    return 1.0 / (1.0 + math.exp(-u))


def fn_phi(u: float) -> float:
    return 0.5 * math.erfc(-u / math.sqrt(2.0))


def fn_tables() -> dict[str, tuple[list[int], int]]:
    """Build the standard 6 function tables."""
    return {
        "exp2": fn_exp2_table(1024),
        "sigmoid": fn_fix_table(fn_sigmoid),
        "phi": fn_fix_table(lambda u: fn_phi(u / 2.0)),
        "recip": fn_pack_segments(fn_mant_segs(lambda u: 1.0 / u, 1024)),
        "rsqrt": fn_pack_segments(fn_half_segs(lambda u: 1.0 / math.sqrt(u), 512)),
        "sqrt": fn_pack_segments(fn_half_segs(math.sqrt, 512)),
    }


MROM_TABLE = fn_mrom_table()


def _fn_fixed_q9_24(x: int, pre: int) -> int:
    """Map E8M15 x to Q9.24 fixed-point v = trunc(x * 2^pre * 2^24), clamped to 2^33 - 1."""
    vmax = (1 << 33) - 1
    s, e, m = e8_fields(x)
    if e == 255:
        mag = vmax
    elif e == 0:
        mag = 0
    else:
        sh = e - 142 + pre + 24
        sig = 0x8000 + m
        mag = (sig << sh) if sh >= 0 else (sig >> -sh)
        mag = min(mag, vmax)
    return -mag if s else mag


def _fn_floor_k(k: int) -> int:
    """floor(k) clamped to [-512, 511]."""
    s, e, _ = e8_fields(k)
    if e == 255:
        return -512 if s else 511
    return max(-512, min(511, math.floor(e8_value(k))))


def _fn_eval_segment(
    table: tuple[list[int], int], seg: int, t: int, n: int, flip: int
) -> int:
    """Evaluate (-1)^(sg ^ flip) * (c0 + c1 * t) * 2^(es + n) and round once to E8M15 (ISA.SR.043)."""
    entries, cs = table
    ent = entries[seg]
    sg = ((ent >> 35) & 1) ^ (flip & 1)
    es = (ent >> 29) & 0x3F
    if es & 0x20:
        es -= 64
    if es == ZERO_ES:
        return sg << 23
    c0 = 0x10000 | ((ent >> 13) & 0xFFFF)
    c1 = ent & 0x1FFF
    if c1 & 0x1000:
        c1 -= 0x2000
    y = (c0 << 13) + c1 * (t << cs)
    val = Fraction(-y if sg else y, 1 << 29) * (Fraction(2) ** (es + n))
    return e8_encode(val, sg)


def fn_eval(
    x: int,
    k: int,
    mode: int,
    pre: int,
    table: tuple[list[int], int] | None,
    rom: bool = False,
    neg: int = 0,
    mrom: tuple[list[int], int] | None = None,
) -> int:
    """Evaluate one SRU function op (ISA.SR.043, sru.md s4)."""
    if mode >= FN_MANT:
        half = mode == FN_HALF
        use_mrom = mrom is not None
        tab = mrom if use_mrom else table
        assert tab is not None
        s, e, m = e8_fields(x)
        if e8_is_nan(x) or (half and s and e != 0):
            return E8_NAN
        if e == 0 or e == 255:
            is_inf_out = (e == 0) == bool(neg)
            return (s << 23) | ((0xFF << 15) if is_inf_out else 0)
        eu = e - 127
        q, r = (eu >> 1, eu & 1) if half else (eu, 0)
        if use_mrom:
            seg = (int(half) << 8) | (r << 7) | (m >> 8)
            t = (m & 0xFF) << 6
        elif half:
            seg = (r << 9) | (m >> 6)
            t = (m & 0x3F) << 8
        else:
            seg = m >> 5
            t = (m & 0x1F) << 9
        n = -q if neg else q
        return _fn_eval_segment(tab, seg, t, n, 0 if half else s)

    if e8_is_nan(x) or (mode == FN_EXP and e8_is_nan(k)):
        return E8_NAN
    assert table is not None
    v = _fn_fixed_q9_24(x, pre)
    if mode == FN_FIX:
        lo, hi = -16 << 24, 16 << 24
        if v < lo:
            seg, t, n = 0, 0, 0
        elif v >= hi:
            seg, t, n = 1023, 0x3FFF, 0
        else:
            u = v - lo
            seg, t, n = u >> 19, (u >> 5) & 0x3FFF, 0
    else:
        w = v - (_fn_floor_k(k) << 24)
        n = w >> 24
        f = w & ((1 << 24) - 1)
        if rom:
            seg, t = f >> 17, (f >> 3) & 0x3FFF
        else:
            seg, t = f >> 14, f & 0x3FFF
    return _fn_eval_segment(table, seg, t, n, 0)


# ---------------------------------------------------------------------------
# Row reductions & finalise ALU (ISA.SR.042, ISA.SR.044, sru.md s3, s5)
# ---------------------------------------------------------------------------


def sign_mag_key(w: int, width: int = 24) -> int:
    """Sign-magnitude total-order key on `width`-bit floats (sru.md s5, ISA.SR.042, ISA.SR.044)."""
    mag_mask = (1 << (width - 1)) - 1
    sign_bit = 1 << (width - 1)
    return ((~w) & mag_mask) if (w & sign_bit) else (sign_bit | (w & mag_mask))


def row_max_e8(words: list[list[int]], r: int) -> int:
    """Per-row maximum of a sub-row's E8M15 words (ISA.SR.042, sru.md s3)."""
    return max(
        (wd[4 * r + c] for wd in words for c in range(4)),
        key=lambda w: sign_mag_key(w, 24),
    )


def row_max_f16(words: list[list[int]], r: int) -> int:
    """Per-row maximum of a sub-row's arriving FP16 words (ISA.SR.042, sru.md s3)."""
    return max(
        (wd[4 * r + c] for wd in words for c in range(4)),
        key=lambda h: sign_mag_key(h, 16),
    )


def row_sum_e8(words: list[list[int]], r: int) -> int:
    """RUL.098 word-ordered sum, independently evaluated on rational grids.

    Element magnitudes truncate toward zero; prior totals floor on growth.
    No behavioural reducer or hardware pipeline implementation is used.
    """
    elems = [wd[4 * r + c] for wd in words for c in range(4)]
    has_nan = any(e8_is_nan(x) for x in elems)
    has_pinf = any(e8_is_inf(x) and not (x >> 23) for x in elems)
    has_ninf = any(e8_is_inf(x) and (x >> 23) for x in elems)
    if has_nan or (has_pinf and has_ninf):
        return E8_NAN
    if has_pinf:
        return 0xFF << 15
    if has_ninf:
        return (1 << 23) | (0xFF << 15)
    total = Fraction(0)
    exponent = 0
    for word in words:
        lane = word[4 * r : 4 * r + 4]
        exponent = max(exponent, *(e8_fields(x)[1] for x in lane))
        quantum = Fraction(2) ** (exponent - 150)
        units = total // quantum
        for x in lane:
            value = e8_value(x)
            magnitude = abs(value) // quantum
            units += -magnitude if value < 0 else magnitude
        # RUL.098 specifies signed 48-bit storage, including wrap.
        units = (units + (1 << 47)) % (1 << 48) - (1 << 47)
        total = units * quantum
    return e8_encode(total, 0)


def fin_alu(op: int, a: int, b: int, c: int) -> int:
    """One scalar finalise op on row scalars (ISA.SR.023, ISA.SR.044, sru.md s5)."""
    if op == FIN_MOV:
        return 0 if e8_is_zero(a) else a
    if op == FIN_ADD:
        return e8_fma(a, E8_ONE, c)
    if op == FIN_SUB:
        return e8_fma(a, E8_ONE, c ^ 0x800000)
    if op == FIN_MUL:
        return e8_fma(a, b, 0, add=False)
    if op == FIN_FMA:
        return e8_fma(a, b, c)
    if op in (FIN_MAX, FIN_MIN):
        ka, kb = sign_mag_key(a, 24), sign_mag_key(b, 24)
        w = a if ((ka >= kb) if op == FIN_MAX else (ka <= kb)) else b
        return 0 if e8_is_zero(w) else w
    if op in (FIN_RCP, FIN_RSQRT):
        mode = FN_MANT if op == FIN_RCP else FN_HALF
        return fn_eval(a, 0, mode, 0, None, neg=1, mrom=MROM_TABLE)
    raise ValueError(f"unknown finalise opcode {op}")


# ---------------------------------------------------------------------------
# Whole-job SRU core evaluation (ISA.SR.040-047)
# ---------------------------------------------------------------------------


def _signed6(v: int) -> int:
    return v - 64 if (v & 0x20) else v


def eval_pass_word(
    ops: list[dict],
    x: list[int],
    b: list[int],
    rows: list[list[int]],
    cols: list[list[int]],
    table: tuple[list[int], int] | None,
    rom: bool,
) -> list[int]:
    """Evaluate a pass program of `ops` on one 16-element sub-tile word (ISA.SR.022, ISA.SR.043)."""
    history: list[list[int]] = []

    def resolve(src_spec, j: int, rsel: int, csel: int, kv: int) -> int:
        if isinstance(src_spec, (tuple, list)):
            return history[src_spec[1]][j]
        if src_spec == "X":
            return x[j]
        if src_spec == "B":
            return b[j]
        if src_spec == "O":
            return x[j ^ 1]
        if src_spec == "K":
            return kv
        if src_spec == "R":
            return rows[j // 4][rsel]
        if src_spec == "C":
            return cols[j % 4][csel]
        raise ValueError(f"unknown operand source {src_spec}")

    for op in ops:
        out_word = []
        for j in range(16):
            av = resolve(op.get("A", "X"), j, 0, 0, 0) ^ (op.get("NA", 0) << 23)
            bv = resolve(
                op.get("B", "K"),
                j,
                op.get("RB", 0),
                op.get("CB", 0),
                op.get("KB", 0),
            )
            if op.get("KIND", 0):
                tab = (table[0], op.get("CS", 0)) if table else None
                pre = _signed6(op.get("PRE", 0))
                mode = 2 * op.get("MANT", 0) + op.get("FIX", 0)
                out_word.append(
                    fn_eval(av, bv, mode, pre, tab, rom=rom, neg=op.get("NEG", 0))
                )
            else:
                cv = resolve(
                    op.get("C", "K"),
                    j,
                    op.get("RC", 0),
                    op.get("CC", 0),
                    op.get("KC", 0),
                ) ^ (op.get("NC", 0) << 23)
                out_word.append(e8_fma(av, bv, cv))
        history.append(out_word)
    return history[-1] if history else list(x)


def pack16(vals: list[int]) -> int:
    """Pack 16 16-bit FP16 values into one 256-bit word."""
    w = 0
    for j, v in enumerate(vals):
        w |= (v & 0xFFFF) << (16 * j)
    return w


def _run_finalise(
    G: dict,
    FIN: list[dict],
    ps: dict,
    W: list[list[int]],
    T: list[int],
    st1: list[int] | None,
    st2: list[int] | None,
) -> None:
    for k in range(ps["FINLEN"]):
        ins = FIN[ps["FINBASE"] + k]
        res = []
        for r in range(4):
            srcs = [
                W[r][0],
                W[r][1],
                W[r][2],
                T[r],
                st1[r] if st1 else 0,
                st2[r] if st2 else 0,
                G["K0"],
                G["K1"],
                G["K2"],
                G["K3"],
            ]
            res.append(
                fin_alu(ins["OP"], srcs[ins["A"]], srcs[ins["B"]], srcs[ins["C"]])
            )
        for r in range(4):
            if ins["DST"] == 3:
                T[r] = res[r]
            else:
                W[r][ins["DST"]] = res[r]


def run_job(
    job: dict,
    a_words: list[list[int]],
    b_words: list[list[int]],
    table: tuple[list[int], int] | None,
    cols_mem: list[list[tuple[int, int]]],
    rows_mem: list[list[int]],
    rom: bool = False,
    ieee: bool = True,
) -> list[int]:
    """Evaluate one SRU job from the specification in ISA.SR.040-047.

    Updates `rows_mem` in place and returns the list of 256-bit output words.
    """
    G, P, FIN, OPS = job["G"], job["P"], job["FIN"], job["OPS"]
    f2e = f16_to_e8_ieee if ieee else f16_to_e8_acc
    e2f = e8_to_f16_ieee if ieee else e8_to_f16_acc
    nw = G["NW"]
    nsr = G["NSR"]
    buf = G["BUF"]
    out_mode = G["OUT"]
    outs: list[int] = []
    mx_subrows: list[list[list[int]]] = []

    for g in range(nsr):
        sr_a = a_words[g * nw : (g + 1) * nw]
        sr_b = b_words[g * nw : (g + 1) * nw] if G["USE_B"] else [[0] * 16] * nw
        row0 = G["ROW0"] + 4 * g
        W = [list(rows_mem[row0 + r]) for r in range(4)]
        T = [0, 0, 0, 0]
        st1 = [f2e(row_max_f16(sr_a, r)) for r in range(4)] if G["R1"] else None

        if buf:
            _run_finalise(G, FIN, P[0], W, T, st1, None)
            passes = range(1, G["NPASS"] + 1)
        else:
            passes = [0]

        for pnum in passes:
            ps = P[pnum]
            ops = OPS[ps["OPBASE"] : ps["OPBASE"] + ps["NOPS"]]
            pass_e8_words: list[list[int]] = []
            fp_out: list[list[int]] = []
            for h, (wd, bw) in enumerate(zip(sr_a, sr_b)):
                x = [f2e(v) for v in wd]
                if G["MASK"]:
                    for j in range(16):
                        col = G["COLOFF"] + 4 * h + (j % 4)
                        row = G["ROWOFF"] + 4 * g + (j // 4)
                        if (col - row) > G["DHI"] or col >= G["CLIM"]:
                            x[j] = G["MVAL"]
                bv = [f2e(v) for v in bw]
                cw = cols_mem[G["COL0"] + h]
                cols = [[f2e(cw[c][0]), f2e(cw[c][1])] for c in range(4)]
                x = eval_pass_word(ops, x, bv, W, cols, table, rom)
                if ps["R2EN"]:
                    pass_e8_words.append(x)
                if ps["EMIT"] and out_mode:
                    fp_out.append([e2f(v) for v in x])

            st2 = (
                [row_sum_e8(pass_e8_words, r) for r in range(4)] if ps["R2EN"] else None
            )
            if fp_out:
                if out_mode == 1:
                    outs.extend(pack16(w) for w in fp_out)
                else:
                    mx_subrows.append(fp_out)
            if buf or ps["FINLEN"]:
                _run_finalise(G, FIN, ps, W, T, st1, st2)

        for r in range(4):
            rows_mem[row0 + r] = W[r]

    if out_mode in (2, 3):
        for fp_out in mx_subrows:
            outs.extend(mx7_subrow_words(fp_out, out_mode == 3))
    return outs
