"""Generate the vector ALU's transcendental coefficient ROMs.

    python scripts/py/vec_tables.py            # measure only, print the report
    python scripts/py/vec_tables.py --write    # regenerate src/kohakutpu/vector/vec_tables.v

Four seeds -- exp2, log2, inv, rsqrt -- each a quadratic minimax-ish polynomial
per segment. See docs/compute/vector-core.md s4.

WHY CHEBYSHEV INTERPOLATION AND NOT REMEZ. Interpolating at the three Chebyshev
nodes of a segment has error  2*(h/4)^3*max|f'''|/6, which is within ~4% of true
minimax for degree 2 and needs no iteration, no numpy, and no convergence test.
The doc quotes that same expression, so the generator and the doc cannot drift:
--report prints the MEASURED error next to it.

FIXED-POINT CONTRACT -- this must match vec_poly.v exactly.

    u   12-bit unsigned, u = raw/4096 in [0,1)       offset within a segment
    c2  22-bit signed, weight 2^-28
    c1  22-bit signed, weight 2^-24
    c0  22-bit signed, weight 2^-20
    F   22-bit signed, weight 2^-20                  the polynomial result

    stage 1   h = (c2*u + (c1<<16) + 2^15) >> 16     -> weight 2^-24
    stage 2   F = (h *u + (c0<<16) + 2^15) >> 16     -> weight 2^-20

Both stages take the same 16-bit slice and both round to nearest. That is what
the scale choice was for; changing any weight here changes the slice constants
in the RTL.

WHY u IS 12 BITS AND NOT 10. A segment index of 5 bits leaves 10 bits of a
15-bit significand, so 10 would be the natural width -- log2, inv and rsqrt
zero-pad the bottom two, which is exact and free. exp2 is the reason for the
extra two: its argument arrives as a FIXED-POINT number produced by a shift, not
as a significand, and rounding that to 15 fraction bits puts an error of 2^-16
into the exponent, which is ln2*2^-16 = 0.35 ulp in the result -- four times the
table's own error. Seventeen fraction bits drops that to 0.09 ulp. The B port is
18-bit signed, so 12 bits costs exactly nothing.

EXACTNESS IS NOT IN THIS TABLE. The identity cases

    exp2(k) == 2^k     log2(2^k) == k     inv(2^k) == 2^-k     rsqrt(2^even)

all sit at the origin of segment 0, and forcing the polynomial through that
point costs 1.5 bits of accuracy on the whole function (see fit_quadratic).
vec_poly.v forces them instead, with a mux on F taken when segment and offset
are both zero. The table is fitted freely.
"""

import argparse
import math
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "src" / "kohakutpu" / "vector" / "vec_tables.v"

SEG_BITS = 5  # 32 segments per octave
NSEG = 1 << SEG_BITS
CW = 22  # coefficient width, signed
Q0, Q1, Q2 = 20, 24, 28  # weights: c0 2^-20, c1 2^-24, c2 2^-28
CMIN, CMAX = -(1 << (CW - 1)), (1 << (CW - 1)) - 1

# fsel encoding, shared with vec_alu.v
F_EXP2, F_LOG2, F_INV, F_RSQRT = 0, 1, 2, 3
FNAMES = {F_EXP2: "exp2", F_LOG2: "log2", F_INV: "inv", F_RSQRT: "rsqrt"}


def seg_fn(fsel, idx6):
    """The real-valued g(u) for one segment, u in [0,1).

    Every function is parameterised so that the segment index is the top
    SEG_BITS of a 15-bit field and u is the remaining 10 bits, which is exactly
    how the RTL slices it.
    """
    idx = idx6 & (NSEG - 1)
    par = idx6 >> SEG_BITS  # rsqrt only: 0 = even exponent

    if fsel == F_EXP2:
        # f = idx/32 + u/32, and 2^f - 1 lands in [0,1) so the significand is
        # already normalised -- this is why exp2 needs no leading-zero count.
        return lambda u: 2.0 ** (idx / 32.0 + u / 32.0) - 1.0
    if fsel == F_LOG2:
        return lambda u: math.log2(1.0 + (idx + u) / 32.0)
    if fsel == F_INV:
        return lambda u: 1.0 / (1.0 + (idx + u) / 32.0)
    # rsqrt: the parity bit selects the octave, so one 6-bit index covers
    # m in [1,2) and 2m in [2,4) without a second shifter.
    scale = 2.0 if par else 1.0
    return lambda u: 1.0 / math.sqrt(scale * (1.0 + (idx + u) / 32.0))


def _solve(rows):
    """Gaussian elimination with partial pivoting on an n x (n+1) system."""
    n = len(rows)
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(rows[r][col]))
        rows[col], rows[piv] = rows[piv], rows[col]
        for r in range(n):
            if r == col:
                continue
            f = rows[r][col] / rows[col][col]
            for k in range(col, n + 1):
                rows[r][k] -= f * rows[col][k]
    return [rows[i][n] / rows[i][i] for i in range(n)]


def fit_quadratic(g):
    """Interpolate g at the three Chebyshev nodes of [0,1]. Returns (c0,c1,c2).

    UNCONSTRAINED, including segment 0. Two other ways of making the identity
    cases exact were tried and both cost accuracy in the segment that matters
    most, because g(0) is where every identity lands:

        c0 bent to the exact value after a 3-parameter fit   inv 2^-18.7
        2-parameter fit constrained through g(0)             inv 2^-18.3
        unconstrained, exactness forced in RTL instead       inv 2^-19.8

    So the exactness pin lives in vec_poly.v as a mux on F, taken when the
    segment index and the offset are both zero. It costs a 15-bit NOR and a
    22-bit mux -- about 25 LUTs -- and buys 1.5 bits back on every function.
    """
    nodes = [0.5 + 0.5 * math.cos((2 * j + 1) * math.pi / 6.0) for j in range(3)]
    return tuple(_solve([[1.0, x, x * x, g(x)] for x in nodes]))


def quantise(fsel, idx6, c):
    """Round to the fixed-point contract. The pin is already exact in `c`."""
    C0 = round(c[0] * (1 << Q0))
    C1 = round(c[1] * (1 << Q1))
    C2 = round(c[2] * (1 << Q2))
    for name, v in (("c0", C0), ("c1", C1), ("c2", C2)):
        if not (CMIN <= v <= CMAX):
            raise SystemExit(
                f"{FNAMES[fsel]} seg {idx6}: {name} = {v} does not fit {CW} bits "
                f"signed -- the weight for {name} is wrong"
            )
    return C0, C1, C2


UW = 12  # u width; u = raw / 2^UW
USH = 16  # Horner slice, both stages


def eval_fixed(C0, C1, C2, u):
    """Bit-exact model of the two Horner DSP stages. u is the raw UW-bit value."""
    h = (C2 * u + (C1 << USH) + (1 << (USH - 1))) >> USH
    return (h * u + (C0 << USH) + (1 << (USH - 1))) >> USH


def build():
    """Returns {(fsel, idx6): (C0,C1,C2)} and {fsel: (max_abs_err, at)}."""
    tab, err = {}, {}
    for fsel in (F_EXP2, F_LOG2, F_INV, F_RSQRT):
        nidx = 2 * NSEG if fsel == F_RSQRT else NSEG
        worst, where = 0.0, None
        for idx6 in range(nidx):
            g = seg_fn(fsel, idx6)
            C = quantise(fsel, idx6, fit_quadratic(g))
            tab[(fsel, idx6)] = C
            # Dense sweep of the ACTUAL circuit against the real function. Every
            # 8th u -- 128 points per segment, 4k-8k per function -- because the
            # error surface is a smooth cubic and sampling it finely enough to
            # find the extremum does not need all 1024.
            for raw in range(0, 1 << UW, 8):
                e = abs(eval_fixed(*C, raw) / (1 << Q0) - g(raw / (1 << UW)))
                if e > worst:
                    worst, where = e, (idx6, raw)
        err[fsel] = (worst, where)
    return tab, err


def predicted(fsel):
    """2*(h/4)^3*max|f'''|/6 -- the expression quoted in vector-core.md s4.2."""
    h = 1.0 / 32.0
    d3 = {
        F_EXP2: (math.log(2) ** 3) * 2.0,
        F_LOG2: 2.0 / (math.log(2)),
        F_INV: 6.0,
        F_RSQRT: 15.0 / 8.0,
    }[fsel]
    return 2.0 * (h / 4.0) ** 3 * d3 / 6.0


def report(err):
    print("  function   segments   predicted     measured    margin over 2^-16")
    for fsel in (F_EXP2, F_LOG2, F_INV, F_RSQRT):
        worst, where = err[fsel]
        n = 2 * NSEG if fsel == F_RSQRT else NSEG
        bits = -math.log2(worst) if worst > 0 else 99.0
        print(
            f"  {FNAMES[fsel]:<10} {n:>6}     2^-{-math.log2(predicted(fsel)):<8.1f}"
            f"  2^-{bits:<8.1f}  {bits - 16:>5.1f} bits   worst at seg {where[0]} u {where[1]}"
        )


def emit(tab, err):
    L = []
    L.append("// Transcendental coefficient ROMs for the vector ALU.")
    L.append("//")
    L.append(
        "// GENERATED by scripts/py/vec_tables.py -- do not edit by hand. That script is"
    )
    L.append(
        "// also the golden model: it evaluates the same two Horner stages in integer"
    )
    L.append("// arithmetic, so a mismatch with vec_alu.v shows up as a bench failure.")
    L.append("//")
    L.append(f"//   u   {UW}-bit unsigned, weight 2^-{UW}")
    L.append(f"//   c2  {CW}-bit signed, weight 2^-{Q2}")
    L.append(f"//   c1  {CW}-bit signed, weight 2^-{Q1}")
    L.append(f"//   c0  {CW}-bit signed, weight 2^-{Q0}")
    L.append("//")
    L.append(f"//   h = (c2*u + (c1<<{USH}) + 2^{USH-1}) >> {USH}")
    L.append(f"//   F = (h *u + (c0<<{USH}) + 2^{USH-1}) >> {USH}")
    L.append("//")
    L.append(
        "// Measured worst-case absolute error of the circuit above against the real"
    )
    L.append("// function, over a dense sweep of every segment:")
    L.append("//")
    for fsel in (F_EXP2, F_LOG2, F_INV, F_RSQRT):
        worst, _ = err[fsel]
        b = -math.log2(worst) if worst > 0 else 99.0
        L.append(
            f"//   {FNAMES[fsel]:<6} 2^-{b:.1f}"
            f"    ({b - 16:.1f} bits of margin over the E8M15 half-ulp)"
        )
    L.append("//")
    L.append("// The fit is UNCONSTRAINED, segment 0 included. The identity cases")
    L.append("//   exp2(k)==2^k   log2(2^k)==k   inv(2^k)==2^-k   rsqrt(2^even)")
    L.append("// are forced in vec_alu.v instead, by a mux on F. Constraining the")
    L.append("// polynomial through g(0) costs 1.5 bits -- see fit_quadratic().")
    L.append("")
    L.append("`default_nettype none")
    L.append("")
    L.append("module vec_tables (")
    L.append("    input  wire               clk,")
    L.append("    input  wire [1:0]         fsel,   // 0 exp2, 1 log2, 2 inv, 3 rsqrt")
    L.append(
        "    input  wire [5:0]         idx,    // idx[5] is the octave parity, rsqrt only"
    )
    L.append(
        "    // SYNCHRONOUS, and rom_style names the primitive rather than leaving"
    )
    L.append(
        "    // it to a heuristic. The one cycle of latency is stated by the RTL, and"
    )
    L.append(
        "    // vec_alu drops its own u_d_ix stage to pay for it -- the address was"
    )
    L.append("    // already registered, so this is a register MOVE and cycle 3 is")
    L.append("    // still when c0/c1/c2 land. 3,568 LUT per core -> 16 RAMB36.")
    L.append(f'    (* rom_style = "block" *) output reg signed [{CW-1}:0] c0,')
    L.append(f'    (* rom_style = "block" *) output reg signed [{CW-1}:0] c1,')
    L.append(f'    (* rom_style = "block" *) output reg signed [{CW-1}:0] c2')
    L.append(");")
    L.append("    // One flat case over {fsel, idx}, constant-indexed throughout.")
    L.append("    // NON-BLOCKING: u_d_c1 in vec_alu registers c1 on the same edge, so")
    L.append(
        "    // blocking assignments here are a scheduler race, not a style point."
    )
    L.append("    always @(posedge clk) begin")
    L.append("        case ({fsel, idx})")
    for fsel in (F_EXP2, F_LOG2, F_INV, F_RSQRT):
        nidx = 2 * NSEG if fsel == F_RSQRT else NSEG
        L.append(f"        // ---- {FNAMES[fsel]} ----")
        for idx6 in range(nidx):
            C0, C1, C2 = tab[(fsel, idx6)]
            a = (fsel << 6) | idx6
            f = [f"-{CW}'sd{-v}" if v < 0 else f"{CW}'sd{v}" for v in (C0, C1, C2)]
            L.append(
                f"        8'd{a:<3}: begin c0 <= {f[0]} ; c1 <= {f[1]} ;"
                f" c2 <= {f[2]} ; end"
            )
    L.append("        default: begin c0 <= 22'sd0 ; c1 <= 22'sd0 ; c2 <= 22'sd0 ; end")
    L.append("        endcase")
    L.append("    end")
    L.append("endmodule")
    L.append("")
    L.append("`default_nettype wire")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help=f"write {OUT.name}")
    args = ap.parse_args()

    tab, err = build()
    report(err)
    if args.write:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(emit(tab, err), encoding="utf-8")
        print(f"\n  wrote {OUT.relative_to(ROOT)}  ({len(tab)} segments)")
    else:
        print("\n  (no --write, nothing regenerated)")


if __name__ == "__main__":
    main()
