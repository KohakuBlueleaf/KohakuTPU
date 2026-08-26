"""IEEE binary32, bit-exact, as KohakuMPE's only compute format.

**There is no E8M15 anywhere in KohakuMPE.** This module replaces
`rv_simd_f16` on every MPE path: the SIMD tier, the SIMT tier, the accumulator
and the converters all read and write FP32, and the arithmetic below is what
`rv_fpu.v` and `khs_fp32_sfu.v` compute, stage for stage. KohakuTPU's vector
core keeps E8M15 and keeps `rv_simd_f16`; the two no longer meet.

Three functions carry the whole tier:

| | |
|---|---|
| `fpu(op, a, b, c)` | `rv_fpu`: one FMA, every opcode routed through it |
| `seed(fsel, a, tab)` | `khs_fp32_sfu`: exp2, log2, rcp, rsqrt |
| `f2i` / `i2f` | `khs_fcvt`: the two integer directions |

**THE MODEL IS THE MACHINE, NOT THE DEFINITION.** `fpu` reproduces the 72-bit
alignment window, its two clamps and its uncomplemented sticky, so a bench can
compare on the BITS. Where that deviates from correct rounding it is written
down at the line that does it; `fma_exact` is the definition, for measuring the
deviation rather than for grading.

Denormals flush to sign-preserved zero on input and output. That is D3D11's
requirement, not a shortcut.
"""

MASK32 = 0xFFFF_FFFF
F32_ONE = 0x3F80_0000
F32_QNAN = 0x7FC0_0000
F32_INF = 0x7F80_0000

#: `rv_fpu`'s opcodes, which `khs_isa.vh` generates as KHS_FOP_* and the SIMT
#: header maps onto. Named once, here, so the model cannot drift from the RTL.
OP_MOV, OP_NEG, OP_ABS = 0, 1, 2
OP_ADD, OP_SUB, OP_MUL = 3, 4, 5
OP_FMA, OP_FNMA = 6, 7
OP_MAX, OP_MIN, OP_SEL = 8, 9, 10
OP_CMPLT, OP_CMPGT, OP_CMPEQ = 11, 12, 13

#: `khs_fp32_sfu`'s function select. The FSFU opcode's low two bits ARE this in
#: both ISAs, so neither caller needs a translation table.
SEED_EXP2, SEED_LOG2, SEED_INV, SEED_RSQRT = 0, 1, 2, 3

SEED_NAME = {
    SEED_EXP2: "vfexp2",
    SEED_LOG2: "vflog2",
    SEED_INV: "vfrcp",
    SEED_RSQRT: "vfrsqrt",
}
SEED_OF = {v: k for k, v in SEED_NAME.items()}


def sig_of(x):
    """The significand `rv_fpu.sig_of` unpacks: zero for a denormal or a zero."""
    return 0 if ((x >> 23) & 0xFF) == 0 else ((1 << 23) | (x & 0x7F_FFFF))


def is_nan(x):
    return ((x >> 23) & 0xFF) == 0xFF and (x & 0x7F_FFFF) != 0


def is_inf(x):
    return ((x >> 23) & 0xFF) == 0xFF and (x & 0x7F_FFFF) == 0


def is_zero(x):
    """True for a real zero AND for a denormal, which flushes to one."""
    return ((x >> 23) & 0xFF) == 0


# ------------------------------------------------------------------ the FMA


def fpu(op, a, b, c):
    """`rv_fpu`, stage for stage. Returns (y, pred).

    Every opcode goes through the one FMA: the unary and select forms pick an
    operand up front and pass it as `winner * 1.0 + 0`, which is bit-exact.
    """
    a &= MASK32
    b &= MASK32
    c &= MASK32

    ae, be, ce = (a >> 23) & 0xFF, (b >> 23) & 0xFF, (c >> 23) & 0xFF
    am, bm = a & 0x7F_FFFF, b & 0x7F_FFFF
    a_nan = ae == 0xFF and am != 0
    b_nan = be == 0xFF and bm != 0
    a_z, b_z = ae == 0, be == 0

    # The compare, on the ORIGINAL bits: IEEE ordering is the integer ordering
    # once the sign is folded. A NEGATIVE IS INVERTED AND NOTHING MORE.
    a_key = (~a) & MASK32 if (a >> 31) & 1 else (a | 0x8000_0000)
    b_key = (~b) & MASK32 if (b >> 31) & 1 else (b | 0x8000_0000)
    cmp_lt = (a_key < b_key) and not (a_z and b_z)
    cmp_eq = (a_key == b_key) or (a_z and b_z)
    cmp_gt = (not cmp_lt) and (not cmp_eq)

    is_cmp = op in (OP_CMPLT, OP_CMPGT, OP_CMPEQ)
    pred = cmp_lt if op == OP_CMPLT else cmp_gt if op == OP_CMPGT else cmp_eq
    pred_q = pred and not (a_nan or b_nan)

    # IEEE maxNum/minNum: a NaN operand LOSES, so one NaN returns the other and
    # two return a quiet NaN through the ordinary specials path. The flush
    # applies to `sel`'s CONDITION too -- a denormal c is zero.
    take_b = (
        ((op == OP_MAX) and (cmp_lt or a_nan) and not b_nan)
        or ((op == OP_MIN) and (cmp_gt or a_nan) and not b_nan)
        or ((op == OP_SEL) and ce == 0)
    )

    if op in (OP_MOV, OP_MAX, OP_MIN, OP_SEL):
        s1a, s1b, s1c = (b if take_b else a), F32_ONE, 0
    elif op == OP_NEG:
        s1a, s1b, s1c = a ^ 0x8000_0000, F32_ONE, 0
    elif op == OP_ABS:
        s1a, s1b, s1c = a & 0x7FFF_FFFF, F32_ONE, 0
    elif op == OP_ADD:
        s1a, s1b, s1c = a, F32_ONE, c
    elif op == OP_SUB:
        s1a, s1b, s1c = a, F32_ONE, c ^ 0x8000_0000
    elif op == OP_MUL:
        s1a, s1b, s1c = a, b, 0
    elif op == OP_FNMA:
        s1a, s1b, s1c = a ^ 0x8000_0000, b, c
    else:
        s1a, s1b, s1c = a, b, c

    pa, pb, pc = sig_of(s1a), sig_of(s1b), sig_of(s1c)
    ps = ((s1a >> 31) & 1) ^ ((s1b >> 31) & 1)
    cs = (s1c >> 31) & 1
    e1a, e1b, e1c = (s1a >> 23) & 0xFF, (s1b >> 23) & 0xFF, (s1c >> 23) & 0xFF
    pz = (e1a == 0) or (e1b == 0)
    cz = e1c == 0

    e_ab = e1a + e1b - 127
    e_c = e1c
    sh_raw = 25 + (e_ab - e_c)
    # Below 0 the addend IS the rounded answer, so the product is zeroed and the
    # exponent rebases on it. Above 96 the addend survives only as a sticky.
    byp = (sh_raw < 0) and not cz
    tiny_c = sh_raw > 96
    sh = 0 if byp else (96 if tiny_c else (sh_raw & 0x7F))

    prod = pa * pb
    stk = 1 if (tiny_c and not cz) else 0
    pz2 = pz or byp
    eab2 = (e_c - 25) if byp else e_ab

    spec_nan = (
        (e1a == 0xFF and (s1a & 0x7F_FFFF) != 0)
        or (e1b == 0xFF and (s1b & 0x7F_FFFF) != 0)
        or (e1c == 0xFF and (s1c & 0x7F_FFFF) != 0)
        or ((e1a == 0xFF or e1b == 0xFF) and pz)
    )
    p_inf = (e1a == 0xFF) or (e1b == 0xFF)
    c_inf_s = (e1c == 0xFF) and ((s1c & 0x7F_FFFF) == 0)
    inf_sub = p_inf and c_inf_s and (ps != cs)
    has_spec = spec_nan or inf_sub or p_inf or c_inf_s
    if spec_nan or inf_sub:
        spec = F32_QNAN
    elif p_inf:
        spec = (ps << 31) | F32_INF
    else:
        spec = (cs << 31) | F32_INF

    # A 72-BIT ALIGNER, and the bits that leave it become a plain sticky. THAT
    # IS RIGHT FOR AN ADDITION AND ONE ULP HIGH FOR A SUBTRACTION with an exact
    # tie above it: the residue would have to be complemented first, and the
    # RTL does not. `fma_exact` measures the difference.
    algn = ((pc << 48) >> sh) & ((1 << 72) - 1)
    lost = 0 if sh <= 48 else (24 if sh > 72 else (sh - 48))
    algn_stk = 1 if (pc & ((1 << lost) - 1)) else 0
    stk |= 1 if (algn_stk and not cz) else 0

    pterm = 0 if pz2 else prod
    cterm = 0 if cz else algn
    if ps != cs:
        c_ge = cterm >= pterm
        mag = (cterm - pterm) if c_ge else (pterm - cterm)
        rsgn = cs if c_ge else ps
    else:
        mag = cterm + pterm
        rsgn = ps
    mag &= (1 << 72) - 1

    nz = mag != 0
    pos = (mag.bit_length() - 1) if nz else 0
    nrm = (mag << (71 - pos)) & ((1 << 72) - 1)

    keep = (nrm >> 48) & 0xFF_FFFF
    guard = (nrm >> 47) & 1
    stick = (1 if (nrm & ((1 << 47) - 1)) else 0) | stk
    sig_r = keep + (guard & (stick | (keep & 1)))
    carry = (sig_r >> 24) & 1
    frac = ((sig_r >> 1) if carry else sig_r) & 0x7F_FFFF
    e_fin = eab2 + pos - 46 + (1 if carry else 0)

    # THE COMPARE OUTRANKS THE SPECIALS: its answer is a boolean, and an inf or
    # a NaN operand is an ordinary input to it.
    if is_cmp:
        y = F32_ONE if pred_q else 0
    elif has_spec:
        y = spec
    elif e_fin >= 255:
        y = (rsgn << 31) | F32_INF
    elif e_fin <= 0 or not nz:
        # AN EXACT CANCELLATION KEEPS THE LOSING TERM'S SIGN HERE, so `x - x`
        # is -0 for a positive x where IEEE gives +0. `rv_fpu` has no `cancels`
        # term; this reproduces it rather than correcting it.
        y = rsgn << 31
    else:
        y = (rsgn << 31) | ((e_fin & 0xFF) << 23) | frac
    return y & MASK32, 1 if pred_q else 0


def fma_exact(a, b, c):
    """a*b + c computed exactly and rounded ONCE -- the definition.

    Not what the hardware does, and not what a bench grades against. It exists
    so the deviation `fpu` reproduces can be measured rather than assumed.
    """
    if is_nan(a) or is_nan(b) or is_nan(c):
        return F32_QNAN
    sp = ((a >> 31) & 1) ^ ((b >> 31) & 1)
    if is_inf(a) or is_inf(b):
        if is_zero(a) or is_zero(b):
            return F32_QNAN
        if is_inf(c) and ((c >> 31) & 1) != sp:
            return F32_QNAN
        return (sp << 31) | F32_INF
    if is_inf(c):
        return c
    pa, pb, pc = sig_of(a), sig_of(b), sig_of(c)
    pe = ((a >> 23) & 0xFF) + ((b >> 23) & 0xFF) - 254 - 46
    ce = ((c >> 23) & 0xFF) - 127 - 23
    if pa == 0 or pb == 0:
        pe = ce
    if pc == 0:
        ce = pe
    lo = min(pe, ce)
    prod = (pa * pb) << (pe - lo)
    add = pc << (ce - lo)
    total = (prod if sp == 0 else -prod) + (add if ((c >> 31) & 1) == 0 else -add)
    if total == 0:
        return 0
    sign = 1 if total < 0 else 0
    mag = abs(total)
    k = mag.bit_length() - 1
    if k >= 23:
        sig = mag >> (k - 23)
        guard = (mag >> (k - 24)) & 1 if k >= 24 else 0
        stick = 1 if (k >= 25 and (mag & ((1 << (k - 24)) - 1))) else 0
    else:
        sig, guard, stick = mag << (23 - k), 0, 0
    if guard & (stick | (sig & 1)):
        sig += 1
        if sig >> 24:
            sig >>= 1
            k += 1
    # `sig` is 1.f * 2^23 and the pack keeps only its low 23 bits, so the biased
    # exponent is k + lo + 127 and NOT that plus 23 -- which reported a third of
    # every stream as one ulp off correct rounding when it was the reference
    # that was wrong.
    e_fin = k + lo + 127
    if e_fin >= 255:
        return (sign << 31) | F32_INF
    if e_fin <= 0:
        return sign << 31
    return (sign << 31) | ((e_fin & 0xFF) << 23) | (sig & 0x7F_FFFF)


# ---------------------------------------------------------------- the seeds


def seed(fsel, a, tab):
    """`khs_fp32_sfu`, stage for stage. `tab` is `khs_seed_tab.TAB`.

    Every function produces `MAG * 2^-32 * 2^X` in a 40-bit field, so one
    leading-one search, one normalising shift and one rounder serve all four,
    and `biased_exp = lead1(MAG) + 95 + X`.
    """
    a &= MASK32
    s = (a >> 31) & 1
    e = (a >> 23) & 0xFF
    m = a & 0x7F_FFFF
    sg = sig_of(a)
    nan_in = e == 0xFF and m != 0
    inf_in = e == 0xFF and m == 0
    zero_in = e == 0
    neg = bool(s) and not zero_in  # -0 is NOT negative here

    if fsel == SEED_EXP2:
        # e >= 134 is |x| >= 128, which leaves binary32's range in one
        # direction or the other; which one is the sign of x.
        big = (e >= 134) or inf_in
        spec_nan = nan_in
        spec_inf = (not nan_in) and (not s) and big
        spec_zero = (not nan_in) and bool(s) and big
        spec_sign = 0
    elif fsel == SEED_LOG2:
        spec_nan = nan_in or neg
        spec_inf = (not spec_nan) and (inf_in or zero_in)
        spec_zero = False
        spec_sign = 1 if zero_in else 0
    elif fsel == SEED_INV:
        spec_nan = nan_in
        spec_inf = (not nan_in) and zero_in
        spec_zero = (not nan_in) and inf_in
        spec_sign = s
    else:
        spec_nan = nan_in or neg
        spec_inf = (not spec_nan) and zero_in
        spec_zero = (not spec_nan) and inf_in
        # rsqrt(-0) IS -inf: IEEE gives sqrt(+-0) = +-0, so 1/sqrt(-0) is 1/-0.
        spec_sign = s if zero_in else 0

    sign = 0
    if fsel == SEED_EXP2:
        # R = x * 2^30, ONE right shift because the bias absorbs the direction.
        sh = 0 if e >= 133 else (133 - e)
        sh = min(sh, 38)
        fld = (sg << 14) >> sh
        rpos = (fld >> 1) + (fld & 1)
        r = -rpos if s else rpos
        k = r >> 30
        f = r & ((1 << 30) - 1)
        idx = (f >> 21) & 0x1FF
        u = f & ((1 << 21) - 1)
        ebase = 95 + k
    elif fsel == SEED_RSQRT:
        # K = floor((e-127)/2); the octave parity is the top index bit, so one
        # table covers [1,2) and [2,4) with no second shifter.
        kk = (e - 127) >> 1
        idx = ((1 - (e & 1)) << 9) | ((m >> 14) & 0x1FF)
        u = (m & 0x3FFF) << 7
        ebase = 95 - kk
    elif fsel == SEED_INV:
        idx = (m >> 14) & 0x1FF
        u = (m & 0x3FFF) << 7
        ebase = 95 + 127 - e
        sign = s
    else:
        idx = (m >> 14) & 0x1FF
        u = (m & 0x3FFF) << 7
        ebase = 95

    c0, c1, c2 = tab[fsel][idx]
    q = c0 + (((c1 + ((c2 * u) >> 22)) * u) >> 20)

    if fsel == SEED_LOG2:
        w = ((e - 127) << 32) + q
        sign = 1 if w < 0 else 0
        mag = -w if w < 0 else w
    else:
        mag = q
    mag &= (1 << 40) - 1

    nz = mag != 0
    pos = (mag.bit_length() - 1) if nz else 0
    nrm = (mag << (39 - pos)) & ((1 << 40) - 1)
    keep = (nrm >> 16) & 0xFF_FFFF
    guard = (nrm >> 15) & 1
    stick = 1 if (nrm & 0x7FFF) else 0
    sig_r = keep + (guard & (stick | (keep & 1)))
    carry = (sig_r >> 24) & 1
    frac = ((sig_r >> 1) if carry else sig_r) & 0x7F_FFFF
    e_fin = ebase + pos + (1 if carry else 0)

    if spec_nan:
        return F32_QNAN
    if spec_inf:
        return (spec_sign << 31) | F32_INF
    if spec_zero:
        return spec_sign << 31
    if not nz or e_fin <= 0:
        return sign << 31
    if e_fin >= 255:
        return (sign << 31) | F32_INF
    return (sign << 31) | ((e_fin & 0xFF) << 23) | frac


# ----------------------------------------------------------- the converters


def f2i(a):
    """binary32 -> int32, toward zero. NaN gives 0, an overflow saturates."""
    a &= MASK32
    s = (a >> 31) & 1
    e = (a >> 23) & 0xFF
    m = a & 0x7F_FFFF
    if e == 0:
        return 0
    if e == 0xFF:
        if m:
            return 0
        return -(1 << 31) if s else (1 << 31) - 1
    # Saturation is decided on the EXPONENT alone: e > 157 is |x| >= 2^31 for
    # every significand, and e <= 157 never reaches it.
    if e > 157:
        return -(1 << 31) if s else (1 << 31) - 1
    sg = (1 << 23) | m
    rs = 182 - e
    v = ((sg << 7) >> (rs - 25)) if rs >= 25 else 0
    return -v if s else v


def i2f(v):
    """int32 -> binary32, round to nearest even. Exact below 2^24."""
    v &= MASK32
    if v == 0:
        return 0
    s = 1 if (v >> 31) & 1 else 0
    mag = ((~v) + 1) & MASK32 if s else v
    k = mag.bit_length() - 1
    nrm = (mag << (31 - k)) & MASK32
    keep = (nrm >> 8) & 0xFF_FFFF
    guard = (nrm >> 7) & 1
    stick = 1 if (nrm & 0x7F) else 0
    sig_r = keep + (guard & (stick | (keep & 1)))
    carry = (sig_r >> 24) & 1
    frac = ((sig_r >> 1) if carry else sig_r) & 0x7F_FFFF
    return (s << 31) | (((k + 127 + carry) & 0xFF) << 23) | frac


# ------------------------------------------------------------------ helpers


def bits(x):
    """A Python float as its binary32 pattern, denormals flushed."""
    import struct

    b = struct.unpack("<I", struct.pack("<f", x))[0]
    return (b & 0x8000_0000) if ((b >> 23) & 0xFF) == 0 else b


def value(b):
    """A binary32 pattern as a Python float."""
    import struct

    return struct.unpack("<f", struct.pack("<I", b & MASK32))[0]


def flush(b):
    """The input flush every MPE float port applies."""
    b &= MASK32
    return (b & 0x8000_0000) if ((b >> 23) & 0xFF) == 0 else b
