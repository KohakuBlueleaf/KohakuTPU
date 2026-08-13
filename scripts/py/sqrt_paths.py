"""Measure the two software square roots as SILICON would compute them.

    python scripts/py/sqrt_paths.py            # the report
    python scripts/py/sqrt_paths.py --octaves 8

`compiler/kohakutpu/model.py` computes `VRSQRT` and `VINV` as `1/np.sqrt(a)` and
`1/a` -- EXACTLY -- so a green SimDevice test says nothing about how accurate a
square root built out of them is on the card. This answers that without the
card, evaluating the seeds as `vec_alu.v` does: the ROM `vec_tables.py`
generates, the same two Horner stages, `ebase`, leading-one search and round.

The seed path is structural, so it reproduces the ALU rather than approximating
it; `VMUL` and `VSUB` are taken as correctly rounded, which is what the ALU
measures -- 0.500 ulp over a 6000-case random sweep and a full 648-case
alignment sweep. `check_seeds` reproduces the same 0.546 and 0.549 ulp, which is
what makes this model creditable.

Error is in ulp of E8M15: a 15-bit significand, so one ulp is 2^-15 relative and
a correctly rounded result is 0.500 by definition.
"""

import argparse
import math

import numpy as np
from vec_tables import F_INV, F_RSQRT, NSEG, Q0, USH, UW, build

#: E8M15, which is `vec_alu.v`'s lane format: sign, 8-bit exponent, 15-bit
#: significand, no subnormals.
SIG_BITS = 15
BIAS = 127

#: One ulp as a fraction of a significand in [1, 2). A correctly rounded value
#: is within half of one.
ULP = 2.0**-SIG_BITS

TAB, TAB_ERR = build()


def _coeffs(fsel):
    """The ROM's three coefficient columns for one function, indexed by segment."""
    n = 2 * NSEG if fsel == F_RSQRT else NSEG
    rows = [TAB[(fsel, i)] for i in range(n)]
    return tuple(np.array([r[k] for r in rows], np.int64) for k in range(3))


INV_C = _coeffs(F_INV)
RSQ_C = _coeffs(F_RSQRT)


# --------------------------------------------------------------- the format
def to_e8(x):
    """`x` rounded to E8M15, as the 24-bit patterns. Round to nearest even.

    Takes and returns arrays. Only finite positive normals are modelled: this
    script sweeps the seeds' interior, and `vec_alu.v` handles zero, infinity
    and the negative half in its specials block rather than in the polynomial.
    """
    x = np.asarray(x, np.float64)
    frac, exp = np.frexp(x)
    # frexp puts the significand in [0.5, 1), so one more bit of headroom than
    # the field holds and the round lands on the field's own last place.
    sig = np.round(frac * float(1 << (SIG_BITS + 1))).astype(np.int64)
    carry = sig >= (1 << (SIG_BITS + 1))
    sig = np.where(carry, sig >> 1, sig)
    exp = np.where(carry, exp + 1, exp)
    return ((exp - 1 + BIAS) << SIG_BITS) | (sig - (1 << SIG_BITS))


def from_e8(bits):
    """The value those 24-bit patterns stand for, as float64."""
    bits = np.asarray(bits, np.int64)
    exp = (bits >> SIG_BITS) & 0xFF
    sig = 1.0 + (bits & 0x7FFF) / float(1 << SIG_BITS)
    return np.ldexp(sig, exp - BIAS)


def e8_op(fn, *xs):
    """One correctly rounded lane operation over E8M15 patterns.

    `VMUL`, `VADD` and `VSUB` all go through the FMA, which
    measures at 0.500 ulp -- correctly rounded -- so rounding the exact result
    is the model.
    """
    return to_e8(fn(*[from_e8(x) for x in xs]))


# ------------------------------------------------- the seeds, as the ALU has them
def _horner(c, u):
    """`vec_alu`'s two Horner DSP stages, bit for bit. `u` is the raw 12-bit offset.

    The same integer arithmetic `vec_tables.eval_fixed` performs, vectorised.
    Both stages take a 16-bit slice and both round to nearest, which is what the
    coefficient weights were chosen for.
    """
    c0, c1, c2 = c
    h = (c2 * u + (c1 << USH) + (1 << (USH - 1))) >> USH
    return (h * u + (c0 << USH) + (1 << (USH - 1))) >> USH


def _assemble(mag, ebase, sign=0):
    """`vec_alu`'s back end: leading-one search, normalise, round, bound.

    `mag` is the fixed-point magnitude and `ebase` the signed exponent base, the
    pair every seed and the FMA alike hand to the shared normaliser. The value
    that comes out is `mag * 2^(ebase - 127)`.
    """
    pos = _lead1(mag)
    nrm = mag << (47 - pos)
    sig = (nrm >> 32) & 0xFFFF
    guard = (nrm >> 31) & 1
    sticky = (nrm & 0x7FFFFFFF) != 0
    up = guard & (sticky | (sig & 1))
    sig_r = sig + up
    carry = (sig_r >> 16) & 1
    frac = np.where(carry.astype(bool), 0, sig_r & 0x7FFF)
    e_fin = pos + ebase + carry
    return (np.int64(sign) << 23) | (e_fin << SIG_BITS) | frac


def _lead1(x):
    """The place of the leading one in a 48-bit magnitude, `mx_lead1`'s `pos`."""
    x = np.asarray(x, np.int64)
    out = np.zeros_like(x)
    for bit in range(48):
        out = np.where((x >> bit) & 1, bit, out)
    return out


def hw_rsqrt(bits):
    """`VRSQRT` as `vec_alu.v` computes it, over E8M15 patterns.

    The octave parity is the table's top index bit, so one 64-entry table covers
    `[1,2)` and `[2,4)` with no second shifter, and `K = floor((e-127)/2)` is an
    arithmetic shift because that is floor for both signs.
    """
    bits = np.asarray(bits, np.int64)
    e = (bits >> SIG_BITS) & 0xFF
    m = bits & 0x7FFF
    par = 1 - (e & 1)
    idx = (par << 5) | (m >> 10)
    k = (e - BIAS) >> 1
    return _pack(RSQ_C, idx, m, 107 - k, ident_extra=par == 0)


def hw_inv(bits):
    """`VINV` as `vec_alu.v` computes it, over E8M15 patterns."""
    bits = np.asarray(bits, np.int64)
    e = (bits >> SIG_BITS) & 0xFF
    m = bits & 0x7FFF
    return _pack(INV_C, m >> 10, m, 234 - e, ident_extra=True)


def _pack(coeff, idx, m, ebase, ident_extra):
    """One seed's polynomial and assembly, given its table, index and exponent base.

    The identity cases -- `inv(2^k)`, `rsqrt(2^even)` -- are forced here as
    `vec_alu.v` forces them, with F pinned to 1.0 when the segment index and the
    offset are both zero. The table itself is fitted freely, which is worth 1.5
    bits everywhere else.
    """
    u = (m & 0x3FF) << 2
    seg = idx & (NSEG - 1)
    f = _horner([c[idx] for c in coeff], u)
    ident = (seg == 0) & (u == 0) & ident_extra
    f = np.where(ident, 1 << Q0, f)
    return _assemble(f.astype(np.int64), np.asarray(ebase, np.int64))


# ------------------------------------------------------------------ the paths
def path_approx(x):
    """`VRSQRT` then `VINV` -- `1/(1/sqrt(x))`, two instruction words."""
    return hw_inv(hw_rsqrt(x))


def path_newton(x):
    """One Newton step on the reciprocal root, six words.

    `y = rsqrt(x)`, `s = x*y`, then `s * (1.5 - 0.5*s*y)`. The correction is
    built out of `VMUL` and `VSUB` alone, so no second approximation enters it
    and the fixed point is the true root to within lane rounding.
    """
    y = hw_rsqrt(x)
    s = e8_op(lambda a, b: a * b, x, y)
    t = e8_op(lambda a, b: a * b, s, y)
    half = e8_op(lambda a, b: a * b, t, to_e8(0.5))
    r = e8_op(lambda a, b: a - b, to_e8(1.5), half)
    return e8_op(lambda a, b: a * b, s, r)


def path_mul(x):
    """`x * VRSQRT(x)` -- the other two-word form, for comparison only.

    NOT offered at the DSL surface: it is NaN at zero and at infinity, where
    `1/(1/sqrt(x))` is 0 and inf, and the measurement below is what says the
    accuracy it buys for that does not exist.
    """
    return e8_op(lambda a, b: a * b, x, hw_rsqrt(x))


def sqrt_table():
    """Fit the ROM an OP_SQRT would need, and price it. Returns a dict.

    Same shape as `rsqrt`'s -- 64 segments, the octave parity as the top index
    bit -- so it reuses `tab_idx` and `rs_k` unchanged and only `ebase` gains an
    arm, `107 + K` beside `107 - K`. `headroom` is how close the largest
    coefficient comes to the 22-bit signed field: sqrt is the only seed whose
    output leaves (0.5, 1], and c0 reaches sqrt(2*(1+31/32)) = 1.984.
    """
    from vec_tables import CMAX, eval_fixed, fit_quadratic

    worst, biggest = 0.0, 0
    for idx6 in range(2 * NSEG):
        scale = 2.0 if idx6 >> 5 else 1.0
        seg = idx6 & (NSEG - 1)

        def g(u, scale=scale, seg=seg):
            return math.sqrt(scale * (1.0 + (seg + u) / 32.0))

        c = fit_quadratic(g)
        fixed = tuple(round(c[k] * (1 << (Q0 + 4 * k))) for k in range(3))
        biggest = max(biggest, max(abs(v) for v in fixed))
        for raw in range(0, 1 << UW, 8):
            worst = max(worst, abs(eval_fixed(*fixed, raw) / (1 << Q0) - g(raw / 4096)))
    return {
        "abs": worst,
        # An output in [1, 2) rather than (0.5, 1], so one binade higher and an
        # absolute error worth HALF what the same error costs the other seeds.
        "ulp": 0.5 + worst * (1 << SIG_BITS),
        "headroom": 1.0 - biggest / CMAX,
    }


def newton_step(x, y):
    """One `y *= 1.5 - 0.5*x*y*y` on an E8M15 reciprocal-root estimate `y`.

    Takes and returns patterns. Written as `s = x*y` then `s*(1.5 - 0.5*s*y)` so
    the same three products serve the estimate and its correction.
    """
    s = e8_op(lambda a, b: a * b, x, y)
    t = e8_op(lambda a, b: a * b, s, y)
    half = e8_op(lambda a, b: a * b, t, to_e8(0.5))
    r = e8_op(lambda a, b: a - b, to_e8(1.5), half)
    return e8_op(lambda a, b: a * b, s, r)


def converge(x, bits: int = 4):
    """Error in ulp per step, from a seed deliberately crippled to `bits`.

    The step refines a RECIPROCAL root but returns a root, so each round feeds
    `s/x` back as the next estimate. Returns one entry per step.
    """
    v = from_e8(x)
    coarse = np.exp2(np.round(np.log2(1.0 / np.sqrt(v)) * (1 << bits)) / (1 << bits))
    y = to_e8(coarse)
    out = []
    for _ in range(4):
        got = newton_step(x, y)
        out.append(ulp_error(got, np.sqrt(v))[0])
        y = to_e8(from_e8(got) / v)
    return out


# ----------------------------------------------------------------- the sweep
def sweep(octaves: int):
    """Every E8M15 pattern over `octaves` octaves centred on 1.0.

    Dense in the significand rather than random: the seeds' error surface is a
    smooth cubic per segment, so its extremum sits at a particular offset and a
    sample would miss it.
    """
    lo = BIAS - octaves // 2
    exps = np.arange(lo, lo + octaves, dtype=np.int64)
    mants = np.arange(1 << SIG_BITS, dtype=np.int64)
    return ((exps[:, None] << SIG_BITS) | mants[None, :]).reshape(-1)


def ulp_error(got_bits, want):
    """Worst error in ulp of E8M15, and the relative error that is.

    Per value, because an ulp is a per-exponent quantity: dividing a whole
    sweep's absolute error by one ulp would report the largest exponent's.
    """
    got = from_e8(got_bits)
    want = np.asarray(want, np.float64)
    scale = np.exp2(np.floor(np.log2(want)) - SIG_BITS)
    return float(np.max(np.abs(got - want) / scale)), float(
        np.max(np.abs(got - want) / want)
    )


def seed_bound(fsel):
    """Half an ulp plus this seed's table error, in ulp -- what the seed must hit.

    `vec_tables` measures the polynomial against the real function in ABSOLUTE
    terms, over a `g` in (0.5, 1]; the assembled result is normalised to [1, 2),
    so that error is worth 2^16 times as much in ulp of the answer.
    """
    worst, _ = TAB_ERR[fsel]
    return 0.5 + worst * (1 << (SIG_BITS + 1))


def check_seeds(x):
    """Each seed's worst error in ulp, against the bound its table error implies.

    Returns ``{name: (measured, bound)}``. Agreement is what says the assembly
    modelled here -- normalise, round, exponent bound -- adds nothing of its own
    beyond the half ulp every correctly rounded result costs.
    """
    v = from_e8(x)
    return {
        "VINV": (ulp_error(hw_inv(x), 1.0 / v)[0], seed_bound(F_INV)),
        "VRSQRT": (ulp_error(hw_rsqrt(x), 1.0 / np.sqrt(v))[0], seed_bound(F_RSQRT)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--octaves", type=int, default=40, help="octaves to sweep")
    args = ap.parse_args()

    x = sweep(args.octaves)
    v = from_e8(x)
    want = np.sqrt(v)
    n = x.size

    print(f"\n  {n:,} E8M15 values over {args.octaves} octaves\n")
    print("  seed check -- measured against 0.5 ulp + this table's own error")
    for name, (got, bound) in check_seeds(x).items():
        print(f"    {name:<9}{got:.3f} ulp   bound {bound:.3f}")
    print(
        "\n  vector-alu.md quotes 0.546 and 0.549 from a 3584-case tb SAMPLE; a\n"
        "  dense sweep finds the segment extremum the sample misses.\n"
    )

    want16 = np.asarray(want, np.float16)
    rows = [
        ("correctly rounded", 1, to_e8(want)),
        ("VRSQRT + VINV", 2, path_approx(x)),
        ("x * VRSQRT", 2, path_mul(x)),
        ("Newton, 1 step", 6, path_newton(x)),
    ]
    print("  path                 words   max ulp   max rel    fp16 ulp   fp16 differs")
    for name, words, got in rows:
        ulp, rel = ulp_error(got, want)
        # fp16 carries 10 significand bits, so its ulp is 32 of this lane's --
        # which is what the error becomes once a VST lands it in memory.
        differs = np.mean(np.asarray(from_e8(got), np.float16) != want16)
        print(
            f"  {name:<20} {words:>4}   {ulp:>7.3f}   {rel:>8.2e}"
            f"   {ulp / 32.0:>8.4f}   {differs:>10.4%}"
        )

    steps = converge(x[:: max(1, n // 4096)])
    made = "  ".join(f"{e:,.0f}" for e in steps)
    print(f"\n  from a 4-bit seed, ulp per Newton step:  {made}")

    hw = sqrt_table()
    print(
        f"\n  an OP_SQRT seed would fit at 2^-{-math.log2(hw['abs']):.1f} absolute"
        f" = {hw['ulp']:.3f} ulp in ONE word,\n  with {hw['headroom']:.1%} of the"
        f" 22-bit coefficient field still spare."
    )


if __name__ == "__main__":
    main()
