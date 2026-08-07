"""Side-by-side matmul in every candidate number format. No simulator needed.

    python compare_formats.py                     16x16x32, worst 24 elements
    python compare_formats.py --m 32 --n 32 --k 64
    python compare_formats.py --all               EVERY element, not a sample
    python compare_formats.py --sort abs          worst by absolute error
    python compare_formats.py --formats fp16,"mxfp8 E4M3","mxfp7 int7 (ours)"

Every format uses FP32 accumulation, so the only thing that differs between
two columns is the OPERAND format. FP64 is the reference.

The per-element table comes first and the distributions come after, because a
median is not an error -- it is a statement about half the matrix. The same run
that has a 2.8% median has individual elements above 300%, and only the table
shows you that.
"""

import argparse

import numpy as np

from kohakutpu import formats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--m", type=int, default=16)
    ap.add_argument("--n", type=int, default=16)
    ap.add_argument("--k", type=int, default=32)
    ap.add_argument("--seed", type=int, default=0xC0FFEE)
    ap.add_argument("--limit", type=int, default=24, help="elements to print")
    ap.add_argument("--all", action="store_true", help="print every element")
    ap.add_argument("--sort", choices=["rel", "abs", "none"], default="rel")
    ap.add_argument("--formats", default="", help="comma-separated subset")
    ap.add_argument(
        "--dist",
        default="lowrank",
        choices=["lowrank", "normal", "uniform", "heavy", "relu"],
        help=(
            "operand distribution. lowrank (default) is CORRELATED, like real "
            "weights and activations. normal is iid, which is the worst case: "
            "every output is a fully cancelled sum, so relative error is pinned "
            "at the operand's own precision no matter how large K is"
        ),
    )
    ap.add_argument("--rank", type=int, default=0, help="lowrank: rank (default K/16)")
    args = ap.parse_args()

    names = (
        [s.strip() for s in args.formats.split(",") if s.strip()]
        if args.formats
        else formats.matmul_names()
    )
    for nm in names:
        if nm not in formats.all_names():
            raise SystemExit(
                f"unknown format {nm!r}; choose from:\n  "
                + "\n  ".join(formats.all_names())
            )

    rng = np.random.default_rng(args.seed)
    k = args.k + (-args.k) % formats.KBLOCK
    if args.dist == "lowrank":
        # CORRELATED operands, which is what real weights and activations are.
        # The signal then accumulates coherently (~K) while quantisation noise
        # stays incoherent (~sqrt K), so relative error falls with K. Under iid
        # operands both grow as sqrt(K) and relative error never improves --
        # which is why an iid benchmark makes every format look far worse than
        # it behaves on real tensors.
        r = args.rank or max(1, k // 16)
        w = rng.standard_normal((r, k))
        a = rng.standard_normal((args.m, r)) @ w
        bt = rng.standard_normal((args.n, r)) @ w
    elif args.dist == "uniform":
        a = rng.uniform(-1, 1, (args.m, k))
        bt = rng.uniform(-1, 1, (args.n, k))
    elif args.dist == "relu":
        a = np.maximum(rng.standard_normal((args.m, k)), 0)
        bt = rng.standard_normal((args.n, k)) * 0.3
    elif args.dist == "heavy":
        # one big outlier per block drags the shared scale up and squeezes
        # everything else -- the case block formats are worst at
        a = rng.standard_normal((args.m, k))
        bt = rng.standard_normal((args.n, k))
        a[:, :: formats.KBLOCK] *= 16.0
        bt[:, :: formats.KBLOCK] *= 16.0
    else:
        a = rng.standard_normal((args.m, k))
        bt = rng.standard_normal((args.n, k))

    want = formats.matmul_fp64(a, bt)
    cols = {nm: formats.matmul_in(nm, a, bt) for nm in names}

    print(
        f"\n  C[{args.m},{args.n}] = A[{args.m},{k}] @ B.T[{args.n},{k}]"
        f"   operands {args.dist}, seed {args.seed}"
    )
    special = [n for n in names if n in formats.SPECIAL_MATMULS]
    note = (
        "  reference: FP64.  every row accumulates in FP32"
        + (f", EXCEPT {', '.join(special)}" if special else "")
        + ".\n"
    )
    print(note)

    # ---------------- per element ----------------
    def rel(g):
        d = np.abs(g - want)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(want != 0, d / np.abs(want), np.inf)

    rels = {nm: rel(c) for nm, c in cols.items()}
    idx = list(np.ndindex(*want.shape))
    key = "int7 + E8M0" if "int7 + E8M0" in cols else names[-1]
    if args.sort == "rel":
        idx.sort(key=lambda p: -(rels[key][p] if np.isfinite(rels[key][p]) else np.inf))
    elif args.sort == "abs":
        idx.sort(key=lambda p: -abs(cols[key][p] - want[p]))

    w = 13
    print(f"  PER-ELEMENT VALUES  (sorted by {args.sort} error of '{key}')")
    head = f"    {'C[i,j]':<11} {'fp64':>{w}}" + "".join(f"{nm:>{w}}" for nm in names)
    print(head)
    print("    " + "-" * (len(head) - 4))
    shown = idx if args.all else idx[: args.limit]
    for p in shown:
        cell = f"C[{p[0]},{p[1]}]"
        row = f"    {cell:<11} {want[p]:{w}.5f}"
        row += "".join(f"{cols[nm][p]:{w}.5f}" for nm in names)
        print(row)
    print()
    print("  PER-ELEMENT RELATIVE ERROR vs FP64  (same elements, same order)")
    head = f"    {'C[i,j]':<11} {'|fp64|':>{w}}" + "".join(f"{nm:>{w}}" for nm in names)
    print(head)
    print("    " + "-" * (len(head) - 4))
    for p in shown:
        cell = f"C[{p[0]},{p[1]}]"
        row = f"    {cell:<11} {abs(want[p]):{w}.5f}"
        for nm in names:
            r = rels[nm][p]
            row += f"{r * 100:{w - 1}.2f}%" if np.isfinite(r) else f"{'inf':>{w}}"
        print(row)
    if not args.all and len(idx) > args.limit:
        print(
            f"    ... {len(idx) - args.limit} of {len(idx)} elements not shown"
            f"  (--all to print them)"
        )

    # ---------------- distributions, after the elements ----------------
    print("\n  DISTRIBUTION OF PER-ELEMENT ERROR  (each sample is one element)")
    print(
        f"    {'format':<20} {'rel p50':>9} {'rel p90':>9} {'rel p99':>9} "
        f"{'rel max':>10} | {'abs p50':>9} {'abs p99':>9} {'abs max':>9} | {'>10%':>6}"
    )
    print("    " + "-" * 106)
    for nm in names:
        r = rels[nm]
        rf = r[np.isfinite(r)]
        d = np.abs(cols[nm] - want)
        over = (rf > 0.10).mean()
        print(
            f"    {nm:<20} {np.percentile(rf, 50):8.2%} {np.percentile(rf, 90):8.2%} "
            f"{np.percentile(rf, 99):8.2%} {rf.max():9.1%} | "
            f"{np.percentile(d, 50):9.3e} {np.percentile(d, 99):9.3e} "
            f"{d.max():9.3e} | {over:5.1%}"
        )
    print(
        "\n    'rel max' is one element, not the format's typical behaviour;"
        "\n    '>10%' is the fraction of elements whose OWN error exceeds 10%."
    )


if __name__ == "__main__":
    main()
