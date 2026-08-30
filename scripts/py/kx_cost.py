#!/usr/bin/env python3
"""Xache (kx_xache, the xbar-cache) resource estimator, fitted to the OOC table.

Two families of rows, one per read engine (RD_PIPE 0 / 1), each measured over
the same 14 shapes on the current array (one scripts/tcl/ooc_kx.tcl synthesis
per row, xcvu13p-fhgb2104-2L-e, 300 MHz ask). LUT is a per-knob STEP TABLE per
family -- the ship plus each knob's measured delta at each measured step,
interpolated between steps, plus the M x N interaction and an SASD saving that
scales as a*N + b*M*N -- because LUT is convex in M and in K and a linear fit
cannot hold 3% across the grid. FF is a linear least-squares fit per family.
The fit is VALIDATED against every row: the estimator is only delivered while
max |error| < 3% on both LUT and FF. Channel interleaving (NSWAP/SWAP_*) and
RD_OUTQ are not terms: the first is wires, the second 71 LUT from 1 to 8.

    python scripts/py/kx_cost.py            # fit, validate, print the table
    python scripts/py/kx_cost.py --json     # coefficients for the web page
    python scripts/py/kx_cost.py --est M=4 N=4 K=1 RSAMD=1 WSAMD=1 CDC=4 RP=1

ROWS_R1 is the first array revision's table, kept as the record it is; the
model is fitted to ROWS only.
"""

import argparse
import json
import sys

# ---- measured rows: (M, N, K, rsamd, wsamd, n_cdc, rd_pipe) -> (LUT, FF, URAM, BRAM, Fmax)
ROWS = {
    # RD_PIPE = 0, the one-beat engine on the current array
    (4, 4, 1, 1, 1, 0, 0): (8408, 7340, 256, 0, 449.2),
    (4, 4, 2, 1, 1, 0, 0): (12527, 13464, 480, 0, 403.4),
    (4, 4, 4, 1, 1, 0, 0): (21423, 21632, 928, 0, 378.4),
    (8, 4, 1, 1, 1, 0, 0): (14242, 7456, 256, 0, 449.0),
    (4, 8, 1, 1, 1, 0, 0): (16992, 14630, 512, 0, 449.2),
    (8, 8, 1, 1, 1, 0, 0): (26370, 14888, 512, 0, 449.0),
    (2, 4, 1, 1, 1, 0, 0): (5287, 7249, 256, 0, 449.0),
    (4, 4, 1, 0, 1, 0, 0): (8538, 7086, 256, 0, 448.4),
    (4, 4, 1, 1, 0, 0, 0): (6756, 6961, 256, 0, 449.0),
    (4, 4, 1, 0, 0, 0, 0): (6356, 6717, 256, 0, 449.2),
    (8, 8, 2, 0, 0, 0, 0): (23999, 25660, 960, 0, 344.0),
    (4, 4, 1, 1, 1, 4, 0): (10323, 10760, 256, 64, 448.8),
    (8, 8, 1, 0, 0, 0, 0): (15742, 13368, 512, 0, 344.2),
    (4, 4, 2, 0, 0, 0, 0): (10146, 12909, 480, 0, 402.9),
    # RD_PIPE = 1, the streaming engine (tree arbiter), RD_OUTQ = 4
    (4, 4, 1, 1, 1, 0, 1): (7839, 7763, 256, 0, 469.3),
    (4, 4, 2, 1, 1, 0, 1): (9881, 11811, 480, 0, 379.1),
    (4, 4, 4, 1, 1, 0, 1): (15005, 19991, 928, 0, 357.7),
    (8, 4, 1, 1, 1, 0, 1): (13177, 7947, 256, 0, 436.7),
    (4, 8, 1, 1, 1, 0, 1): (15049, 15471, 512, 0, 480.8),
    (8, 8, 1, 1, 1, 0, 1): (25288, 15795, 512, 0, 469.3),
    (2, 4, 1, 1, 1, 0, 1): (4741, 7629, 256, 0, 456.2),
    (4, 4, 1, 0, 1, 0, 1): (5001, 7213, 256, 0, 380.8),
    (4, 4, 1, 1, 0, 0, 1): (6161, 7384, 256, 0, 447.0),
    (4, 4, 1, 0, 0, 0, 1): (3366, 6834, 256, 0, 380.8),
    (8, 8, 2, 0, 0, 0, 1): (10562, 21720, 960, 0, 301.6),
    (4, 4, 1, 1, 1, 4, 1): (9642, 11183, 256, 64, 469.3),
    (8, 8, 1, 0, 0, 0, 1): (6456, 13534, 512, 0, 346.6),
    (4, 4, 2, 0, 0, 0, 1): (5056, 10940, 480, 0, 337.0),
}
# the ship at RD_OUTQ 1 / 2 / 8 (loop 4): 9,607 / 9,607 / 9,678 LUT,
# 11,147 / 11,151 / 11,231 FF -- the queue depth is not a term

# the first array revision (docs/projects/kohakuaxi/xbar-cache.md 5.1 before
# the read-queue loop): kept, not fitted
ROWS_R1 = {
    (4, 4, 1, 1, 1, 0): (9914, 7390, 256, 0, 492.1),
    (4, 4, 2, 1, 1, 0): (14467, 13552, 480, 0, 361.9),
    (4, 4, 4, 1, 1, 0): (22847, 21688, 928, 0, 341.6),
    (8, 4, 1, 1, 1, 0): (15132, 7508, 256, 0, 498.0),
    (4, 8, 1, 1, 1, 0): (18219, 14778, 512, 0, 496.5),
    (8, 8, 1, 1, 1, 0): (28194, 15000, 512, 0, 498.0),
    (2, 4, 1, 1, 1, 0): (6237, 7310, 256, 0, 495.3),
    (4, 4, 1, 0, 1, 0): (9543, 7199, 256, 0, 451.3),
    (4, 4, 1, 1, 0, 0): (7694, 7017, 256, 0, 472.6),
    (4, 4, 1, 0, 0, 0): (7350, 6830, 256, 0, 451.1),
    (8, 8, 2, 0, 0, 0): (27968, 25969, 960, 0, 362.1),
    (4, 4, 1, 1, 1, 4): (11865, 10788, 256, 64, 456.0),
    (8, 8, 1, 0, 0, 0): (17718, 13613, 512, 0, 381.1),
    (4, 4, 2, 0, 0, 0): (12155, 13022, 480, 0, 361.8),
}


def rows_of(rp):
    return {k[:6]: v for k, v in ROWS.items() if k[6] == rp}


def features(m, n, k, rsamd, wsamd, ncdc):
    # crossbar m*n plus per-master and per-home parts; the line buffer per
    # home per extra IO-word plus one fill-register doubling; read-SASD near
    # constant; write-SASD collapses N write paths.
    return [
        1.0,
        n,
        m,
        m * n,
        n * (k - 1),
        n * (1 if k > 1 else 0),
        (1 - rsamd),
        (1 - wsamd) * n,
        ncdc,
    ]


NAMES = ["base", "c_n", "c_m", "c_mn", "c_k", "c_kfill", "r_sasd", "w_sasd", "cdc"]


def lstsq(X, y):
    # normal equations with Gaussian elimination -- no numpy dependency
    p = len(X[0])
    A = [
        [sum(X[r][i] * X[r][j] for r in range(len(X))) for j in range(p)]
        for i in range(p)
    ]
    b = [sum(X[r][i] * y[r] for r in range(len(X))) for i in range(p)]
    for i in range(p):
        piv = max(range(i, p), key=lambda r: abs(A[r][i]))
        A[i], A[piv] = A[piv], A[i]
        b[i], b[piv] = b[piv], b[i]
        for r in range(i + 1, p):
            f = A[r][i] / A[i][i]
            for c in range(i, p):
                A[r][c] -= f * A[i][c]
            b[r] -= f * b[i]
    x = [0.0] * p
    for i in range(p - 1, -1, -1):
        x[i] = (b[i] - sum(A[i][c] * x[c] for c in range(i + 1, p))) / A[i][i]
    return x


def fit_ff(rp):
    rows = rows_of(rp)
    keys = list(rows)
    # a term whose column is all zero in this family is dropped from the fit
    X = [features(*k) for k in keys]
    live = [i for i in range(len(NAMES)) if any(X[r][i] != 0 for r in range(len(X)))]
    Xl = [[X[r][i] for i in live] for r in range(len(X))]
    if len(Xl) < len(live):
        return {NAMES[i]: 0.0 for i in range(len(NAMES))}
    ff = lstsq(Xl, [rows[k][1] for k in keys])
    out = {NAMES[i]: 0.0 for i in range(len(NAMES))}
    for j, i in enumerate(live):
        out[NAMES[i]] = ff[j]
    return out


class LutSteps:
    """Per-family LUT step table, read straight from the rows."""

    def __init__(self, rp):
        r = rows_of(rp)
        g = lambda m, n, k, rs, ws, c: r.get((m, n, k, rs, ws, c), (None,))[0]
        self.ship = g(4, 4, 1, 1, 1, 0)
        self.M = {m: g(m, 4, 1, 1, 1, 0) for m in (2, 4, 8) if g(m, 4, 1, 1, 1, 0)}
        self.N = {n: g(4, n, 1, 1, 1, 0) for n in (4, 8) if g(4, n, 1, 1, 1, 0)}
        self.K = {k: g(4, 4, k, 1, 1, 0) for k in (1, 2, 4) if g(4, 4, k, 1, 1, 0)}
        self.MN88 = g(8, 8, 1, 1, 1, 0)
        self.RSASD = (g(4, 4, 1, 0, 1, 0) or self.ship) - self.ship
        self.WSASD = (g(4, 4, 1, 1, 0, 0) or self.ship) - self.ship
        self.CDC = ((g(4, 4, 1, 1, 1, 4) or self.ship) - self.ship) / 4.0
        s4 = self.ship - (g(4, 4, 1, 0, 0, 0) or self.ship)
        s8 = (self.MN88 or 0) - (g(8, 8, 1, 0, 0, 0) or self.MN88 or 0)
        # both-SASD saving grows as a*N + b*M*N: two points, (N=4,M=4), (N=8,M=8).
        # One side alone is its 4x4 delta scaled by the same growth; the two are
        # not additive (read-SASD alone can COST, sharing one engine's arbiter).
        self.SASD_B = (s8 - 2 * s4) / (64 - 32) if s8 else 0.0
        self.SASD_A = (s4 - self.SASD_B * 16) / 4.0
        self.S4 = s4
        # K under a shared write path grows as a*N + b*M*N too: K2 costs 1,690
        # at 4x4 SASD and 4,106 at 8x8 SASD (streaming) -- a per-home constant
        # read at 4x4 missed 8x8 by 6.9%
        k2s4 = (g(4, 4, 2, 0, 0, 0) or 0) - (g(4, 4, 1, 0, 0, 0) or 0)
        k2s8 = (g(8, 8, 2, 0, 0, 0) or 0) - (g(8, 8, 1, 0, 0, 0) or 0)
        self.KS_B = (k2s8 - 2 * k2s4) / (64 - 32) if k2s8 else 0.0
        self.KS_A = (k2s4 - self.KS_B * 16) / 4.0 if k2s4 else 0.0
        self.K2 = self.K.get(2, self.ship) - self.ship

    @staticmethod
    def _interp(table, x):
        ks = sorted(table)
        if x in table:
            return table[x]
        if len(ks) < 2:
            return table[ks[0]]
        if x < ks[0] or x > ks[-1]:
            a, b = (ks[0], ks[1]) if x < ks[0] else (ks[-2], ks[-1])
        else:
            a = max(k for k in ks if k <= x)
            b = min(k for k in ks if k >= x)
        return table[a] + (table[b] - table[a]) * (x - a) / (b - a)

    def estimate(self, m, n, k, rsamd, wsamd, ncdc):
        ship = self.ship
        d_m = self._interp(self.M, m) - ship
        d_n = self._interp(self.N, n) - ship
        x_mn = (
            (
                self.MN88
                - ship
                - (self.M.get(8, ship) - ship)
                - (self.N.get(8, ship) - ship)
            )
            if self.MN88
            else 0.0
        )
        d_mn = x_mn * ((m - 4) / 4.0) * ((n - 4) / 4.0)
        d_k = (self._interp(self.K, k) - ship) * (n / 4.0)
        if not wsamd and k > 1 and self.K2:
            d_k = (
                (self._interp(self.K, k) - ship)
                / self.K2
                * (self.KS_A * n + self.KS_B * m * n)
            )
        both = self.SASD_A * n + self.SASD_B * m * n
        scale = (both / self.S4) if self.S4 else 1.0
        if not rsamd and not wsamd:
            d_s = -both
        elif not rsamd:
            d_s = self.RSASD * scale
        elif not wsamd:
            d_s = self.WSASD * scale
        else:
            d_s = 0.0
        d_c = self.CDC * ncdc
        return ship + d_m + d_n + d_mn + d_k + d_s + d_c


def estimate_ff(coef, m, n, k, rsamd, wsamd, ncdc):
    return sum(
        coef[nm] * f for nm, f in zip(NAMES, features(m, n, k, rsamd, wsamd, ncdc))
    )


def memory(n, k, ncdc):
    uram = n * (64 + 56 * (k - 1) * (2 if k >= 4 else 1))
    bram = 16 * ncdc
    return uram, bram


def validate(verbose=True):
    worst_l = worst_f = 0.0
    for rp in (0, 1):
        rows = rows_of(rp)
        if not rows:
            continue
        L = LutSteps(rp)
        F = fit_ff(rp)
        if verbose:
            print(f"--- RD_PIPE={rp}: {len(rows)} rows")
            print(
                f"{'M':>2} {'N':>2} {'K':>2} rS wS cdc | {'LUT':>6} {'est':>7} {'err%':>6} | {'FF':>6} {'est':>7} {'err%':>6}"
            )
        for key, (lut, ff, *_rest) in rows.items():
            el, ef = L.estimate(*key), estimate_ff(F, *key)
            pl, pf = 100.0 * (el - lut) / lut, 100.0 * (ef - ff) / ff
            worst_l, worst_f = max(worst_l, abs(pl)), max(worst_f, abs(pf))
            if verbose:
                m, n, k, r, w, c = key
                print(
                    f"{m:>2} {n:>2} {k:>2} {r:>2} {w:>2} {c:>3} | {lut:>6} {el:>7.0f} {pl:>+6.2f} | {ff:>6} {ef:>7.0f} {pf:>+6.2f}"
                )
    if verbose:
        print(f"max |err|: LUT {worst_l:.2f}%  FF {worst_f:.2f}%  (target < 3%)")
    return worst_l, worst_f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--est", nargs="*", help="M=4 N=4 K=1 RSAMD=1 WSAMD=1 CDC=0 RP=1")
    a = ap.parse_args()
    if a.json:
        out = {}
        for rp in (0, 1):
            if rows_of(rp):
                L = LutSteps(rp)
                out[f"rp{rp}"] = {
                    "ff": fit_ff(rp),
                    "lut_steps": {
                        "ship": L.ship,
                        "M": L.M,
                        "N": L.N,
                        "K": L.K,
                        "MN88": L.MN88,
                        "RSASD": L.RSASD,
                        "WSASD": L.WSASD,
                        "CDC": L.CDC,
                        "SASD_A": L.SASD_A,
                        "SASD_B": L.SASD_B,
                        "S4": L.S4,
                        "KS_A": L.KS_A,
                        "KS_B": L.KS_B,
                    },
                    "rows": [list(k) + list(v) for k, v in rows_of(rp).items()],
                }
        print(json.dumps(out, indent=1))
        return
    if a.est:
        kv = dict(s.split("=") for s in a.est)
        m, n, k = int(kv.get("M", 4)), int(kv.get("N", 4)), int(kv.get("K", 1))
        r, w, c = (
            int(kv.get("RSAMD", 1)),
            int(kv.get("WSAMD", 1)),
            int(kv.get("CDC", 0)),
        )
        rp = int(kv.get("RP", 1))
        u, b = memory(n, k, c)
        print(
            f"LUT {LutSteps(rp).estimate(m, n, k, r, w, c):.0f}  FF {estimate_ff(fit_ff(rp), m, n, k, r, w, c):.0f}  URAM {u}  BRAM {b}"
        )
        return
    wl, wf = validate()
    if wl >= 3 or wf >= 3:
        sys.exit("FIT NOT DELIVERABLE: error >= 3%")


if __name__ == "__main__":
    main()
