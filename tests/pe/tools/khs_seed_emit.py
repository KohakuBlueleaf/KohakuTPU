"""Fit and emit `khs_fp32_sfu`'s coefficient table.

    python tests/pe/tools/khs_seed_emit.py            # write
    python tests/pe/tools/khs_seed_emit.py --check    # regenerate and compare

One quadratic per segment, 256 segments over the reduced argument and 512 for
rsqrt's two octaves. The evaluated form is INTEGER and is the RTL's:

    Q = C0 + (((C1 + ((C2 * U) >> 22)) * U) >> 20)

so the model and the lane cannot round differently and the bench compares on
the bits. `t = U * 2^-30` in every function, which is what makes those two
shifts constants rather than a per-function mux.

C0 is the EXACT value at the segment origin, never a fitted one. That costs a
little on the fit and buys exp2(k), log2(2^k), rcp(2^k) and rsqrt(2^2k) exactly,
which is the property a shader notices.

The generated `khs_seed_tab.v` is the only artifact: `khs_seed_tab.py` parses
it, so the RTL and the model cannot hold different numbers.
"""

import argparse
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT_V = ROOT / "src" / "kohakumpe" / "simd" / "generated" / "khs_seed_tab.v"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import khs_fp32 as K

#: Segments per function. rsqrt indexes the octave parity as its top bit.
#: 512 measures 1.000 ULP on all four, which is the ROUNDING floor and not the
#: fit -- no table beats it. The old "256 gives 1.98 and 1.84" reading came from
#: the metric `ulp_of` replaced and is not a reason for anything; 512 stays
#: because the tables cost 0 LUT either way and this one is verified.
LEN = (512, 512, 512, 1024)
STRIDE = 1024
H = 2.0**-9

C0_W, C1_W, C2_W = 34, 24, 16


def seg_fun(fsel, idx):
    """F(t) on segment `idx`, exact in float64 over the segment's own range."""
    if fsel == K.SEED_EXP2:
        base = idx / 512.0
        return lambda t: 2.0 ** (base + t)
    if fsel == K.SEED_LOG2:
        v0 = 1.0 + idx / 512.0
        return lambda t: math.log2(v0 + t)
    if fsel == K.SEED_INV:
        v0 = 1.0 + idx / 512.0
        return lambda t: 1.0 / (v0 + t)
    v0 = 1.0 + (idx & 0x1FF) / 512.0
    scale = 2.0 ** (-0.5 * (idx >> 9))
    return lambda t: scale / math.sqrt(v0 + t)


def poly(c0, c1, c2, u):
    """The RTL's evaluation, integer for integer."""
    return c0 + (((c1 + ((c2 * u) >> 22)) * u) >> 20)


def fit_segment(fsel, idx, samples):
    """(C0, C1, C2) for one segment, C0 exact and the other two refined."""
    f = seg_fun(fsel, idx)
    c0 = round(f(0.0) * 2.0**32)

    # Least squares of the residual against [t, t^2], in Q units. The model is
    # C1*t*2^10 + C2*t^2*2^18, so the fitted coefficients divide straight down.
    pts = [H * (i + 0.5) / 96.0 for i in range(96)]
    s11 = s12 = s22 = b1 = b2 = 0.0
    for t in pts:
        r = f(t) * 2.0**32 - c0
        t2 = t * t
        s11 += t2
        s12 += t2 * t
        s22 += t2 * t2
        b1 += r * t
        b2 += r * t2
    det = s11 * s22 - s12 * s12
    a = (b1 * s22 - b2 * s12) / det
    b = (b2 * s11 - b1 * s12) / det
    c1 = round(a / 2.0**10)
    c2 = round(b / 2.0**18)

    # Refine against the exact integer pipeline: rounding two real coefficients
    # independently is not the best integer pair, and the neighbourhood is 9.
    want = [(u, f(u * 2.0**-30) * 2.0**32) for u in samples]
    best, arg = None, (c1, c2)
    for d1 in (-1, 0, 1):
        for d2 in (-1, 0, 1):
            err = max(abs(poly(c0, c1 + d1, c2 + d2, u) - w) for u, w in want)
            if best is None or err < best:
                best, arg = err, (c1 + d1, c2 + d2)
    return (c0, arg[0], arg[1])


def samples_for(fsel):
    """U values the refinement grades on: the whole span, endpoints included."""
    top = 1 << 21
    out = list(range(0, top, top // 192))
    out.append(top - 1)
    if fsel != K.SEED_EXP2:
        # The 15-bit functions only ever see multiples of 2^7.
        out = sorted({(u >> 7) << 7 for u in out})
    return out


def build_table():
    tab = []
    for fsel in range(4):
        smp = samples_for(fsel)
        tab.append([fit_segment(fsel, i, smp) for i in range(LEN[fsel])])
    return tab


HEAD = """\
// GENERATED from tests/pe/tools/khs_seed_emit.py -- DO NOT EDIT.
// Regenerate with `python tests/pe/tools/khs_seed_emit.py`; the check gate
// regenerates and compares, so a hand edit here fails rather than quietly
// disagreeing with the golden model, which PARSES THIS FILE.
//
// One quadratic per segment of the reduced argument, evaluated as
//     Q = C0 + (((C1 + ((C2 * U) >> 22)) * U) >> 20)
// where Q is the function scaled by 2^32 and U is t * 2^30. C0 is the exact
// value at the segment origin, so the identity cases are exact.
//
// fsel 0 exp2 (512), 1 log2 (512), 2 rcp (512), 3 rsqrt (1024, octave on top).

`default_nettype none

module khs_seed_tab (
    input  wire        clk,
    input  wire [1:0]  fsel,
    input  wire [9:0]  idx,
    output reg  signed [33:0] c0,
    output reg  signed [23:0] c1,
    output reg  signed [15:0] c2
);
    // THREE ARRAYS, NOT ONE 74-BIT WORD. A block-RAM port is 72 bits at its
    // widest, so a 74-bit array cannot map and Vivado DISCARDS `rom_style`
    // silently: MEASURED 2,798 LUT / 0 BRAM packed against 0 LUT / 9 RAMB36
    // split. `ram_style` and dropping the zero-fill changed nothing.
"""

ARR = """\
    (* rom_style = "block" *)
    reg [%d:0] %s [0:4095];
    initial begin
"""

TAIL = """\
    always @(posedge clk) begin
        c0 <= m0[{fsel, idx}];
        c1 <= m1[{fsel, idx}];
        c2 <= m2[{fsel, idx}];
    end
endmodule

`default_nettype wire
"""

#: name, width, and which coefficient of the fitted triple it holds.
BANKS = ((0, "m0", C0_W), (1, "m1", C1_W), (2, "m2", C2_W))


def render(tab):
    out = [HEAD]
    for k, nm, w in BANKS:
        out.append("\n")
        out.append(ARR % (w - 1, nm))
        for fsel in range(4):
            for i, cs in enumerate(tab[fsel]):
                v = cs[k] & ((1 << w) - 1)
                out.append(
                    "        %s[%4d] = %d'h%0*x;\n"
                    % (nm, fsel * STRIDE + i, w, (w + 3) // 4, v)
                )
        out.append("    end\n")
    out.append("\n")
    out.append(TAIL)
    return "".join(out)


# ------------------------------------------------------------------- report


def ulp_of(x):
    """One ULP at `x`: 2^(exponent - 23).

    NOT `relative * 2^24`. One ULP is 2^-23 of a value whose significand is near
    1.0 and 2^-24 of one near 2.0, so a relative measure over-reports by up to
    two -- worst exactly at a power of two, which is where a seed's worst case
    tends to land. It reported every ONE-ulp result as two, and the number then
    did not move when the table was made eight times finer, because a one-ulp
    result is the ROUNDING floor rather than the approximation.
    """
    b = K.bits(abs(x))
    e = (b >> 23) & 0xFF
    return 2.0 ** (e - 150) if e else 2.0**-149


def ulp_report(tab):
    """Max ULP error of the whole seed against float64, per function.

    `log2` near 1 is excluded: its result goes to zero there, so it is an
    absolute-error function and a relative measure reports infinity rather than
    a defect.
    """
    worst = {}
    for fsel in range(4):
        name = K.SEED_NAME[fsel]
        top, arg = 0.0, 0
        for step in (9973, 40961, 1048573):
            for n in range(0, 1 << 23, step):
                if fsel == K.SEED_EXP2:
                    a = K.bits(-60.0 + 120.0 * (n / float(1 << 23)))
                else:
                    # The exponent walks with n; the mantissa is n's own bits.
                    e = 100 + (n % 55)
                    a = (e << 23) | (n & 0x7F_FFFF)
                x = K.value(a)
                if fsel == K.SEED_EXP2:
                    want = 2.0**x
                elif fsel == K.SEED_LOG2:
                    if abs(x - 1.0) < 0.02:
                        continue
                    want = math.log2(x)
                elif fsel == K.SEED_INV:
                    want = 1.0 / x
                else:
                    want = 1.0 / math.sqrt(x)
                got = K.value(K.seed(fsel, a, tab))
                if want == 0.0 or not math.isfinite(want):
                    continue
                if not math.isfinite(got):
                    continue
                ref = K.value(K.bits(want))
                if ref == 0.0:
                    continue
                ulp = abs(got - ref) / ulp_of(ref)
                if ulp > top:
                    top, arg = ulp, a
        worst[name] = (top, arg)
    return worst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="compare, do not write")
    ap.add_argument("--report", action="store_true", help="measure the ULP error")
    args = ap.parse_args()

    tab = build_table()
    text = render(tab)

    if args.check:
        was = OUT_V.read_text(encoding="utf-8") if OUT_V.exists() else ""
        if was != text:
            print("  FAIL -- khs_seed_tab.v is stale; rerun khs_seed_emit.py")
            return 1
        print("  PASS -- %d seed coefficients match" % sum(LEN))
        return 0

    OUT_V.parent.mkdir(parents=True, exist_ok=True)
    OUT_V.write_text(text, encoding="utf-8")
    print("  wrote %d coefficients to %s" % (sum(LEN), OUT_V))

    if args.report:
        for name, (ulp, a) in ulp_report(tab).items():
            print("  %-8s max %.3f ULP   worst input %08x" % (name, ulp, a))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
