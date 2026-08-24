"""E8M15 and its conversions, bit-exact, as the float tier's reference.

The SIMD PE's float tier keeps FP16 in the register file and computes in E8M15,
exactly as the vector core does at its own load and store edges. A golden model
for that tier therefore has to reproduce `vec_cvt.v` bit for bit -- not
approximately, and never as float64 with a tolerance, because a tolerance hides
precisely the rounding bugs a float datapath has.

Formats, all as plain integers:

    FP16    s |  e5  |      m10        16 bits
    E8M15   s |  e8  |      m15        24 bits, no subnormals, no implicit range check
    FP32    s |  e8  |      m23        32 bits

The two structural facts everything here rests on, both from the vector core's
design: **FP16 -> E8M15 is exact** (an 8-bit exponent covers FP16's range with
room, and a subnormal normalises into an ordinary value), and **E8 is FP32's
exponent field verbatim**, so E8M15 -> FP32 is exact and free.

`selftest()` proves the first one over the whole FP16 space rather than
asserting it.
"""

# --------------------------------------------------------------- helpers


def _lead1(x: int, width: int):
    """Position of the most significant set bit, and whether there is one."""
    for b in range(width - 1, -1, -1):
        if x & (1 << b):
            return b, True
    return 0, False


# ----------------------------------------------------------- conversions


def f16_to_e8(f16: int) -> int:
    """FP16 -> E8M15. Exact for every input, subnormals included."""
    s = (f16 >> 15) & 1
    e5 = (f16 >> 10) & 0x1F
    m10 = f16 & 0x3FF

    if e5 == 0 and m10 == 0:
        return s << 23
    if e5 == 0x1F:  # inf or NaN
        return (s << 23) | (0xFF << 15) | (m10 << 5)
    if e5 == 0:
        # value = m10 * 2^-24 = 2^(p-24) * 1.f, so the exponent is p + 103 and
        # shifting the leading one up to bit 15 leaves the fraction below it.
        p, _ = _lead1(m10, 10)
        sub_e = 103 + p
        sub_sh = (m10 << (15 - p)) & 0xFFFF
        return (s << 23) | (sub_e << 15) | (sub_sh & 0x7FFF)
    return (s << 23) | ((e5 + 112) << 15) | (m10 << 5)


def e8_to_f16(e8: int) -> int:
    """E8M15 -> FP16. Rounds to nearest even; a finite overflow SATURATES.

    Saturation rather than infinity is the vector core's choice and the matmul
    accumulator's: two different answers for one overflow is worse than the loss.
    """
    s = (e8 >> 23) & 1
    e = (e8 >> 15) & 0xFF
    m = e8 & 0x7FFF
    sig = (1 << 15) | m

    if e == 0:
        return s << 15
    if e == 0xFF:
        if m != 0:  # NaN, quietened
            return (s << 15) | (0x1F << 10) | (1 << 9) | ((m >> 5) & 0x1FF)
        return (s << 15) | (0x1F << 10)

    keep = (sig >> 5) & 0x7FF
    guard = (m >> 4) & 1
    stick = 1 if (m & 0xF) else 0
    rnd = keep + (guard & (stick | (keep & 1)))
    carry = (rnd >> 11) & 1

    e5_adj = (e - 112) + carry
    n_man = 0 if carry else (rnd & 0x3FF)

    if e5_adj >= 31:  # saturate to the largest finite
        return (s << 15) | (0x1E << 10) | 0x3FF
    if e5_adj <= 0:
        sh = min(118 - e, 31)
        wide = (sig << 32) >> sh
        sub_k = (wide >> 32) & 0x7FF
        sub_g = (wide >> 31) & 1
        sub_s = 1 if (wide & 0x7FFFFFFF) else 0
        sub_r = sub_k + (sub_g & (sub_s | (sub_k & 1)))
        # A rounding carry lands on bit 10, which IS the exponent field's LSB,
        # so this becomes the smallest normal with no fixup.
        return (s << 15) | (sub_r & 0x7FF)
    return (s << 15) | ((e5_adj & 0x1F) << 10) | n_man


def f32_to_e8(f32: int) -> int:
    """FP32 -> E8M15. The exponent is kept verbatim; the significand rounds 23 -> 15."""
    s = (f32 >> 31) & 1
    e = (f32 >> 23) & 0xFF
    m = f32 & 0x7FFFFF

    if e == 0:  # E8M15 has no subnormals
        return s << 23
    if e == 0xFF:
        if m != 0:
            return (s << 23) | (0xFF << 15) | (1 << 14) | ((m >> 8) & 0x3FFF)
        return (s << 23) | (0xFF << 15)

    keep = (m >> 8) & 0x7FFF
    guard = (m >> 7) & 1
    stick = 1 if (m & 0x7F) else 0
    rnd = keep + (guard & (stick | (keep & 1)))
    carry = (rnd >> 15) & 1
    e_adj = e + carry
    n_man = 0 if carry else (rnd & 0x7FFF)

    if e_adj >= 255:  # the one overflow E8 cannot hold
        return (s << 23) | (0xFF << 15)
    return (s << 23) | (e_adj << 15) | n_man


def e8_to_f32(e8: int) -> int:
    """E8M15 -> FP32. Exact, and free: the exponent is unchanged."""
    if ((e8 >> 15) & 0xFF) == 0:
        return ((e8 >> 23) & 1) << 31
    return (((e8 >> 23) & 1) << 31) | (((e8 >> 15) & 0xFF) << 23) | ((e8 & 0x7FFF) << 8)


# ------------------------------------------------------------------- FMA

E8_NAN = (0xFF << 15) | (1 << 14)


def e8_parts(x: int):
    """(sign, exponent, 16-bit significand, class) for an E8M15 word."""
    s = (x >> 23) & 1
    e = (x >> 15) & 0xFF
    m = x & 0x7FFF
    if e == 0:
        return s, 0, 0, "zero"
    if e == 0xFF:
        return s, e, m, ("nan" if m else "inf")
    return s, e, (1 << 15) | m, "num"


def e8_fma(a: int, b: int, c: int, negate: bool = False) -> int:
    """a*b + c in E8M15, computed exactly and rounded ONCE, to nearest even.

    Written from the definition rather than from the lane's pipeline: the lane
    aligns into a 48-bit window and clamps the shift at both ends, and both
    clamps are claimed to preserve correct rounding. A model that reproduced the
    pipeline instead would agree with the hardware about its own mistakes.

    E8M15 has no subnormals, so a result below the smallest normal flushes to
    zero, and one at or above 2^128 becomes an infinity.
    """
    sa, ea, siga, ka = e8_parts(a)
    sb, eb, sigb, kb = e8_parts(b)
    sc, ec, sigc, kc = e8_parts(c)
    if negate:
        sc ^= 1

    if ka == "nan" or kb == "nan" or kc == "nan":
        return E8_NAN
    sp = sa ^ sb
    if ka == "inf" or kb == "inf":
        if (ka == "zero") or (kb == "zero"):  # inf * 0
            return E8_NAN
        if kc == "inf" and sc != sp:  # inf - inf
            return E8_NAN
        return (sp << 23) | (0xFF << 15)
    if kc == "inf":
        return (sc << 23) | (0xFF << 15)

    # Exact: the product is an integer at weight 2^pe, the addend at 2^ce.
    prod, pe = siga * sigb, (ea - 127) + (eb - 127) - 30
    add, ce = sigc, (ec - 127) - 15
    if ka == "zero" or kb == "zero":
        prod, pe = 0, ce
    if kc == "zero":
        add, ce = 0, pe

    lo = min(pe, ce)
    total = (prod << (pe - lo)) * (1 if sp == 0 else -1) + (add << (ce - lo)) * (
        1 if sc == 0 else -1
    )

    if total == 0:
        return 0  # an exact cancellation is +0
    sign = 1 if total < 0 else 0
    mag = abs(total)

    k = mag.bit_length() - 1  # value = 1.f * 2^(k+lo)
    if k >= 15:
        sig = mag >> (k - 15)
        guard = (mag >> (k - 16)) & 1 if k >= 16 else 0
        stick = 1 if (k >= 17 and (mag & ((1 << (k - 16)) - 1))) else 0
    else:
        sig = mag << (15 - k)
        guard = stick = 0

    if guard & (stick | (sig & 1)):
        sig += 1
        if sig >> 16:  # rounded up to 2.0
            sig >>= 1
            k += 1

    e_fin = k + lo + 127
    if e_fin >= 255:
        return (sign << 23) | (0xFF << 15)
    if e_fin <= 0:
        return sign << 23
    return (sign << 23) | (e_fin << 15) | (sig & 0x7FFF)


def e8_fma_hw(a: int, b: int, c: int) -> int:
    """a*b + c as the LANE computes it, alignment window and all.

    `e8_fma` above is the definition and is correctly rounded. This is the
    machine: `vec_alu` aligns the addend into a 48-bit window with the product
    at bits 31:0, clamps the shift at both ends, and carries the bits that fall
    outside as a plain sticky.

    THE STICKY IS WHERE THE TWO PART. For an addition, bits shifted out make the
    true sum larger and a sticky says exactly that. For a SUBTRACTION they make
    it smaller, so the residue would have to be complemented first -- and it is
    not, here or in `mx_fpacc`. The result is at most one ulp high, only when
    subtracting with bits outside the window. A golden model has to reproduce
    that or it cannot be bit-exact against the hardware; the difference from
    `e8_fma` is the measure of the deviation.
    """
    sa, ea, siga, ka = e8_parts(a)
    sb, eb, sigb, kb = e8_parts(b)
    sc, ec, sigc, kc = e8_parts(c)

    if ka == "nan" or kb == "nan" or kc == "nan":
        return E8_NAN
    sp = sa ^ sb
    if ka == "inf" or kb == "inf":
        if ka == "zero" or kb == "zero":
            return E8_NAN
        if kc == "inf" and sc != sp:
            return E8_NAN
        return (sp << 23) | (0xFF << 15)
    if kc == "inf":
        return (sc << 23) | (0xFF << 15)
    if ka == "zero" or kb == "zero":
        return c
    if kc == "zero":
        return e8_fma(a, b, 0)

    s = ea + eb - ec - 110
    if s < 0:
        # The product is below half an ulp of the addend: the addend already IS
        # the correctly rounded answer, and the lane emits it.
        return c
    prod = siga * sigb  # 32 bits, at field bits 31:0
    if s > 48:
        shifted, lost = 0, 1
    else:
        wide = sigc << 32
        shifted = wide >> s
        lost = 1 if (wide & ((1 << s) - 1)) else 0

    if sp == sc:
        mag, sign = prod + shifted, sp
    else:
        d = prod - shifted
        if d >= 0:
            mag, sign = d, sp
        else:
            mag, sign = -d, sc

    if mag == 0:
        return 0
    k = mag.bit_length() - 1
    if k >= 15:
        sig = mag >> (k - 15)
        guard = (mag >> (k - 16)) & 1 if k >= 16 else 0
        stick = lost | (1 if (k >= 17 and (mag & ((1 << (k - 16)) - 1))) else 0)
    else:
        sig, guard, stick = mag << (15 - k), 0, lost

    if guard & (stick | (sig & 1)):
        sig += 1
        if sig >> 16:
            sig >>= 1
            k += 1

    e_fin = k + (ea + eb - 284) + 127
    if e_fin >= 255:
        return (sign << 23) | (0xFF << 15)
    if e_fin <= 0:
        return sign << 23
    return (sign << 23) | (e_fin << 15) | (sig & 0x7FFF)


# ------------------------------------- the product, in accumulator format


def f16_prod_acc(a_f16: int, b_f16: int, mw: int = 16) -> int:
    """FP16 x FP16 -> S1 E7 M<mw>, following khs_float_prod.v step for step.

    This mirrors the RTL rather than the definition ON PURPOSE: `prod_acc_exact`
    below is the definition, and the two are compared. A single model would
    prove only that it agrees with itself.
    """
    ae8, be8 = f16_to_e8(a_f16), f16_to_e8(b_f16)
    sa, ea = (ae8 >> 23) & 1, (ae8 >> 15) & 0xFF
    sb, eb = (be8 >> 23) & 1, (be8 >> 15) & 0xFF
    siga = (1 << 15) | (ae8 & 0x7FFF)
    sigb = (1 << 15) | (be8 & 0x7FFF)
    sgn = sa ^ sb

    if ea == 0 or eb == 0:
        return 0
    if ea == 0xFF or eb == 0xFF:
        return (sgn << (mw + 7)) | (127 << mw) | ((1 << mw) - 1)

    prod = siga * sigb
    up = (prod >> 31) & 1
    keep = (prod >> 15) & 0x1FFFF if up else (prod >> 14) & 0x1FFFF
    gd = (prod >> 14) & 1 if up else (prod >> 13) & 1
    st = 1 if (prod & ((1 << 14) - 1)) else 0
    if not up:
        st = 1 if (prod & ((1 << 13) - 1)) else 0

    drop = 16 - mw
    trunc = keep >> drop
    if drop == 0:
        r_g, r_s = gd, st
    else:
        r_g = (keep >> (drop - 1)) & 1
        r_s = st | gd | (1 if (drop > 1 and (keep & ((1 << (drop - 1)) - 1))) else 0)

    rnd = trunc + (r_g & (r_s | (trunc & 1)))
    rcy = (rnd >> (mw + 1)) & 1
    man = 0 if rcy else (rnd & ((1 << mw) - 1))
    e7 = ea + eb + up - 191 + rcy

    if e7 <= 0:
        return 0
    if e7 >= 127:
        return (sgn << (mw + 7)) | (127 << mw) | ((1 << mw) - 1)
    return (sgn << (mw + 7)) | (e7 << mw) | man


def prod_acc_exact(a_f16: int, b_f16: int, mw: int = 16) -> int:
    """The same product from the DEFINITION: exact, then rounded once."""
    ae8, be8 = f16_to_e8(a_f16), f16_to_e8(b_f16)
    sa, ea, siga, ka = e8_parts(ae8)
    sb, eb, sigb, kb = e8_parts(be8)
    sgn = sa ^ sb
    if ka == "zero" or kb == "zero":
        return 0
    if ka != "num" or kb != "num":
        return (sgn << (mw + 7)) | (127 << mw) | ((1 << mw) - 1)

    mag = siga * sigb  # exact, 32 bits
    lo = (ea - 127) + (eb - 127) - 30  # value = mag * 2^lo
    k = mag.bit_length() - 1

    sig = mag >> (k - mw) if k >= mw else mag << (mw - k)
    guard = (mag >> (k - mw - 1)) & 1 if k > mw else 0
    stick = 1 if (k > mw + 1 and (mag & ((1 << (k - mw - 1)) - 1))) else 0
    if guard & (stick | (sig & 1)):
        sig += 1
        if sig >> (mw + 1):
            sig >>= 1
            k += 1

    e7 = k + lo + 63
    if e7 <= 0:
        return 0
    if e7 >= 127:
        return (sgn << (mw + 7)) | (127 << mw) | ((1 << mw) - 1)
    return (sgn << (mw + 7)) | (e7 << mw) | (sig & ((1 << mw) - 1))


# ------------------------------------------------- the accumulator's add


def acc_parts(x: int, mw: int):
    """(sign, exponent, mantissa) of an S1 E7 M<mw> word. Exponent 0 is zero."""
    return (x >> (mw + 7)) & 1, (x >> mw) & 0x7F, x & ((1 << mw) - 1)


def acc_add(a: int, b: int, mw: int = 16) -> int:
    """S1 E7 M<mw> addition, following mx_fpacc_add step for step.

    Eight guard bits and a sticky, one rounding, and a finite overflow that
    SATURATES rather than becoming an infinity -- the accumulator format has no
    infinity, and this is where a GEMM's range dies rather than silently
    becoming NaN later.
    """
    guard, sw = 8, mw + 9
    sa, ea, ma = acc_parts(a, mw)
    sb, eb, mb = acc_parts(b, mw)
    if ea == 0 and eb == 0:
        return 0
    if ea == 0:
        return b
    if eb == 0:
        return a

    a_ge = (ea > eb) or (ea == eb and ma >= mb)
    e_big = ea if a_ge else eb
    s_big = sa if a_ge else sb
    s_sml = sb if a_ge else sa
    diff = (ea - eb) if a_ge else (eb - ea)

    bg = ((1 << mw) | (ma if a_ge else mb)) << guard
    sml = ((1 << mw) | (mb if a_ge else ma)) << guard

    if diff >= sw:
        shifted, lost = 0, (1 if sml else 0)
    else:
        shifted = sml >> diff
        lost = 1 if (sml & ((1 << diff) - 1)) else 0

    total = (bg + shifted) if s_big == s_sml else (bg - shifted)
    if total == 0:
        return 0

    lz = sw - (total.bit_length() - 1)
    norm = total << lz
    e_out = e_big + 1 - lz

    sig = (norm >> (sw - mw)) & ((1 << (mw + 1)) - 1)
    g = (norm >> (sw - mw - 1)) & 1
    st = lost | (1 if (norm & ((1 << (sw - mw - 1)) - 1)) else 0)
    up = g & (st | (sig & 1))

    if up and sig == (1 << (mw + 1)) - 1:
        sig = 1 << mw
        e_out += 1
    else:
        sig += up

    if e_out <= 0:
        return 0
    if e_out >= 127:
        return (s_big << (mw + 7)) | (0x7F << mw) | ((1 << mw) - 1)
    return (s_big << (mw + 7)) | ((e_out & 0x7F) << mw) | (sig & ((1 << mw) - 1))


def acc_to_e8(x: int, mw: int = 16) -> int:
    """S1 E7 M<mw> -> E8M15, and it is RANGE-LOSSLESS by construction.

    E7 spans real exponents -63..64 and E8 spans -126..127, so `e8 = e7 + 64`
    always lands inside: there is no overflow path, no saturation and no range
    check. The significand rounds to nearest even when mw > 15 and zero-extends
    when it is smaller.
    """
    s, e, m = acc_parts(x, mw)
    if e == 0:
        return s << 23
    if mw <= 15:
        return (s << 23) | ((e + 64) << 15) | (m << (15 - mw))

    sig = (1 << mw) | m
    drop = mw - 15
    keep = sig >> drop
    guard = (sig >> (drop - 1)) & 1
    stick = 1 if (sig & ((1 << (drop - 1)) - 1)) else 0
    if guard & (stick | (keep & 1)):
        keep += 1
        if keep >> 16:
            keep >>= 1
            e += 1
    return (s << 23) | (((e + 64) & 0xFF) << 15) | (keep & 0x7FFF)


def acc_value(x: int, mw: int):
    """The exact value of an accumulator word, as (numerator, exponent)."""
    s, e, m = acc_parts(x, mw)
    if e == 0:
        return 0, 0
    return ((1 << mw) | m) * (-1 if s else 1), e - 63 - mw


# ---------------------------------------------------------------- checks


def selftest() -> int:
    """Every claim above, over the whole FP16 space. Returns the error count."""
    bad = 0

    # 1. FP16 round-trips EXACTLY -- the property the whole arrangement rests on.
    #    NaNs are the one exception and they quieten rather than change value.
    for f in range(1 << 16):
        e5, m10 = (f >> 10) & 0x1F, f & 0x3FF
        back = e8_to_f16(f16_to_e8(f))
        if e5 == 0x1F and m10 != 0:
            if ((back >> 10) & 0x1F) != 0x1F or (back & 0x3FF) == 0:
                bad += 1
                print("  NaN did not stay a NaN: %04x -> %04x" % (f, back))
        elif back != f:
            bad += 1
            if bad < 10:
                print("  round trip: %04x -> %04x" % (f, back))

    # 2. FP16 -> E8M15 -> FP32 equals FP16 -> FP32, so the two paths into the
    #    wider format agree. Checked through Python's own float, which is exact
    #    for both directions here.
    import struct

    for f in range(1 << 16):
        e5, m10 = (f >> 10) & 0x1F, f & 0x3FF
        if e5 == 0x1F:
            continue
        s = -1.0 if (f >> 15) else 1.0
        want = (
            (s * m10 * 2.0**-24)
            if e5 == 0
            else (s * (1.0 + m10 / 1024.0) * 2.0 ** (e5 - 15))
        )
        got = struct.unpack("<f", struct.pack("<I", e8_to_f32(f16_to_e8(f))))[0]
        if got != want:
            bad += 1
            if bad < 20:
                print("  value: %04x -> %r want %r" % (f, got, want))

    # 3. The FMA against Python's own arithmetic, on values where float64 is
    #    exact for the whole product-sum: FP16 significands are 11 bits, so a
    #    product is 22 and the sum stays well inside float64's 53.
    import random

    rng = random.Random(20260821)
    for _ in range(20000):
        fa, fb, fc = (rng.randrange(1 << 16) for _ in range(3))
        if any(((f >> 10) & 0x1F) == 0x1F for f in (fa, fb, fc)):
            continue
        va, vb, vc = (
            struct.unpack("<e", struct.pack("<H", f))[0] for f in (fa, fb, fc)
        )
        got = e8_fma(f16_to_e8(fa), f16_to_e8(fb), f16_to_e8(fc))
        want = va * vb + vc
        gotv = struct.unpack("<f", struct.pack("<I", e8_to_f32(got)))[0]
        # A TOLERANCE IS RIGHT HERE AND NOWHERE ELSE. This checks the model
        # against float64, which carries 53 significand bits where E8M15 carries
        # 16, so the two cannot agree bit for bit by construction. What has to be
        # bit-exact is model against RTL, and that comparison is the bench's.
        if want == 0.0:
            ok = (got & 0x7FFFFF) == 0
        else:
            ok = abs(gotv - want) <= abs(want) * 2.0**-16
        if not ok:
            bad += 1
            if bad < 20:
                print("  fma: %04x %04x %04x -> %r want %r" % (fa, fb, fc, gotv, want))

    # 4. khs_float_prod's algorithm against the exact product, at both mantissa
    #    widths. The RTL normalises with a one-bit shift because a product of
    #    two normalised significands can only be [1,4); if that reasoning is
    #    wrong anywhere, these disagree.
    for mw in (14, 16):
        n = 0
        for _ in range(30000):
            fa, fb = rng.randrange(1 << 16), rng.randrange(1 << 16)
            if f16_prod_acc(fa, fb, mw) != prod_acc_exact(fa, fb, mw):
                bad += 1
                n += 1
                if n < 5:
                    print(
                        "  prod mw=%d: %04x * %04x -> %06x want %06x"
                        % (
                            mw,
                            fa,
                            fb,
                            f16_prod_acc(fa, fb, mw),
                            prod_acc_exact(fa, fb, mw),
                        )
                    )
        # Edges as well as randoms: 1.0, the largest and smallest normals, the
        # smallest subnormal, and zero, against each other.
        edges = [0x3C00, 0x7BFF, 0x0400, 0x0001, 0x0000, 0xBC00, 0xFBFF]
        for fa in edges:
            for fb in edges:
                if f16_prod_acc(fa, fb, mw) != prod_acc_exact(fa, fb, mw):
                    bad += 1
                    print(
                        "  prod edge mw=%d: %04x * %04x -> %06x want %06x"
                        % (
                            mw,
                            fa,
                            fb,
                            f16_prod_acc(fa, fb, mw),
                            prod_acc_exact(fa, fb, mw),
                        )
                    )

    # 5. Algebraic identities the FMA must satisfy whatever it is made of.
    #    These need no reference implementation at all, which is what makes them
    #    worth more than another comparison against float64.
    one, zero = f16_to_e8(0x3C00), 0
    for _ in range(20000):
        fa, fb, fc = (rng.randrange(1 << 16) for _ in range(3))
        if any(((f >> 10) & 0x1F) == 0x1F for f in (fa, fb, fc)):
            continue
        a, b, c = f16_to_e8(fa), f16_to_e8(fb), f16_to_e8(fc)
        # ZERO IS EXCLUDED FROM THE TWO IDENTITY LAWS, and not as a convenience:
        # the sum of two zeros of opposite sign is +0 in round-to-nearest, so
        # (-0)*1 + (+0) is +0 and NOT -0. A model that returned -0 there would be
        # wrong, and the identity as stated would have called that right.
        nz_a = (a & 0x7FFFFF) != 0
        nz_c = (c & 0x7FFFFF) != 0
        checks = [
            ("a*1+0 == a", e8_fma(a, one, zero), a if nz_a else 0),
            ("0*b+c == c", e8_fma(zero, b, c), c if nz_c else 0),
            ("commutative", e8_fma(a, b, c), e8_fma(b, a, c)),
            ("sign symmetry", e8_fma(a, b, c), e8_fma(a ^ (1 << 23), b ^ (1 << 23), c)),
            # a*1 is EXACT, so subtracting a cancels completely -- and the
            # answer is +0. The sign of an exact cancellation is a decision, not
            # a don't-care: the lane forces it positive and so does this.
            ("a*1 - a == +0", e8_fma(a, one, a ^ (1 << 23)), 0),
        ]
        for name, got, want in checks:
            if got != want:
                bad += 1
                if bad < 25:
                    print(
                        "  identity %s: %04x %04x %04x -> %06x want %06x"
                        % (name, fa, fb, fc, got, want)
                    )

    # 6. The accumulator add against exact integer arithmetic. Independent by
    #    construction: this path never shifts, never rounds early, and never
    #    knows what a guard bit is -- it adds two exact rationals and rounds the
    #    answer once.
    ulp_off = 0
    for mw in (14, 16):
        for _ in range(20000):
            fa, fb = rng.randrange(1 << 16), rng.randrange(1 << 16)
            a = f16_prod_acc(fa, 0x3C00, mw)  # an ordinary accumulator word
            b = f16_prod_acc(fb, 0x3C00, mw)
            got = acc_add(a, b, mw)

            (na, ka), (nb, kb) = acc_value(a, mw), acc_value(b, mw)
            if na == 0 and nb == 0:
                want = 0
            else:
                lo = min(ka, kb) if (na and nb) else (ka if na else kb)
                tot = (na << (ka - lo)) + (nb << (kb - lo))
                if tot == 0:
                    want = 0
                else:
                    sgn, mag = (1 if tot < 0 else 0), abs(tot)
                    k = mag.bit_length() - 1
                    sig = mag >> (k - mw) if k >= mw else mag << (mw - k)
                    gd = (mag >> (k - mw - 1)) & 1 if k > mw else 0
                    sk = 1 if (k > mw + 1 and (mag & ((1 << (k - mw - 1)) - 1))) else 0
                    if gd & (sk | (sig & 1)):
                        sig += 1
                        if sig >> (mw + 1):
                            sig >>= 1
                            k += 1
                    e = k + lo + 63
                    want = (
                        0
                        if e <= 0
                        else (
                            ((sgn << (mw + 7)) | (0x7F << mw) | ((1 << mw) - 1))
                            if e >= 127
                            else (sgn << (mw + 7)) | (e << mw) | (sig & ((1 << mw) - 1))
                        )
                    )
            if got != want:
                # The one KNOWN deviation, and it is the accumulator's, not
                # this model's: `lost` is OR-ed into the sticky, which is right
                # for an addition and wrong for a SUBTRACTION -- the discarded
                # bits make the true result smaller than the computed one, so
                # the residue has to be complemented before it rounds. The
                # error is bounded at one ulp and only occurs subtracting with
                # bits shifted out. Anything else here is a real disagreement.
                sa_, ea_, _ = acc_parts(a, mw)
                sb_, eb_, _ = acc_parts(b, mw)
                subtractive = (sa_ != sb_) and ea_ and eb_
                if subtractive and abs(got - want) == 1:
                    ulp_off += 1
                else:
                    bad += 1
                    if bad < 25:
                        print(
                            "  acc_add mw=%d: %06x + %06x -> %06x want %06x"
                            % (mw, a, b, got, want)
                        )

    # 7. A finite overflow SATURATES rather than becoming an infinity.
    big = f32_to_e8(struct.unpack("<I", struct.pack("<f", 1.0e30))[0])
    if e8_to_f16(big) != 0x7BFF:
        bad += 1
        print("  1e30 -> f16 gave %04x, want 7bff (largest finite)" % e8_to_f16(big))

    if ulp_off:
        print(
            "  note: %d of 40000 accumulator adds are 1 ulp off "
            "correct rounding -- subtractive alignment with lost bits, "
            "which mx_fpacc rounds as if the residue were positive" % ulp_off
        )
    print("%s -- %d wrong" % ("PASS" if bad == 0 else "FAIL", bad))
    return bad


if __name__ == "__main__":
    raise SystemExit(1 if selftest() else 0)
