"""Per-element error reporting.

EVERY row is ONE element: its own value under each format, its own absolute
error, its own relative error against its own reference. Nothing is divided by
the tensor peak or by any other element.

Aggregates appear only AFTER the elements, never instead of them, and never
alone. A median hides the tail by construction: the same run that has a 2.8%
median has elements at 372% and 8918%, and quoting the median as "the error"
is simply false about those elements.
"""

import numpy as np


def _rel(got, want):
    a = np.abs(got - want)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(want != 0, a / np.abs(want), np.inf)


def elements(got, refs, limit=None, sort="rel", against="fp64"):
    """One line per element: value under every format, plus err vs `against`.

    ``refs`` maps a name to an array of the same shape as ``got``.
    ``limit=None`` prints every element -- which is the point.
    """
    got = np.asarray(got, dtype=np.float64)
    base = np.asarray(refs[against], dtype=np.float64)
    abs_e = np.abs(got - base)
    rel_e = _rel(got, base)

    idx = list(np.ndindex(*got.shape))
    if sort == "rel":
        idx.sort(key=lambda p: -(rel_e[p] if np.isfinite(rel_e[p]) else np.inf))
    elif sort == "abs":
        idx.sort(key=lambda p: -abs_e[p])

    names = list(refs)
    head = f"    {'C[i,j]':<11} {'hardware':>12}"
    head += "".join(f" {n:>12}" for n in names)
    head += f" {'abs err':>11} {'rel err':>11}"
    lines = [head, "    " + "-" * (len(head) - 4)]

    shown = idx if limit is None else idx[:limit]
    for p in shown:
        r = rel_e[p]
        rs = f"{r * 100:10.2f}%" if np.isfinite(r) else "       inf"
        cell = f"C[{p[0]},{p[1]}]"
        row = f"    {cell:<11} {got[p]:12.5f}"
        row += "".join(f" {np.asarray(refs[n])[p]:12.5f}" for n in names)
        row += f" {abs_e[p]:11.3e} {rs:>11}"
        lines.append(row)
    if limit is not None and len(idx) > limit:
        lines.append(f"    ... {len(idx) - limit} of {len(idx)} elements not shown")
    return lines


def distribution(got, want, label=""):
    """The SHAPE of the per-element error, spelled out rather than summarised.

    Percentiles and a worst case together, because either alone misleads: the
    median says most elements are fine, the max says one is terrible, and only
    both say how many are which.
    """
    g = np.asarray(got, dtype=np.float64).ravel()
    w = np.asarray(want, dtype=np.float64).ravel()
    a = np.abs(g - w)
    r = _rel(g, w)
    rf = r[np.isfinite(r)]
    if rf.size == 0:
        rf = np.zeros(1)
    n = rf.size

    over = [(t, int((rf > t).sum())) for t in (0.01, 0.05, 0.10, 0.50, 1.00)]
    return [
        f"    {label}",
        (
            f"      abs err   p50 {np.percentile(a, 50):.3e}  p90 {np.percentile(a, 90):.3e}"
            f"  p99 {np.percentile(a, 99):.3e}  worst {a.max():.3e}"
        ),
        (
            f"      rel err   p50 {np.percentile(rf, 50):7.2%}  p90 {np.percentile(rf, 90):7.2%}"
            f"  p99 {np.percentile(rf, 99):7.2%}  worst {rf.max():.1%}"
        ),
        "      elements over  "
        + "  ".join(f"{int(t * 100)}%: {c} ({c / n:.1%})" for t, c in over),
    ]


def compare(got, refs, against="fp64", limit=24):
    """Full report: worst elements first, then each format's own distribution."""
    lines = [f"  worst elements by relative error (hardware vs {against}):"]
    lines += elements(got, refs, limit=limit, sort="rel", against=against)
    lines.append("")
    lines.append("  worst elements by absolute error:")
    lines += elements(got, refs, limit=limit, sort="abs", against=against)
    lines.append("")
    lines.append(f"  per-element error distributions, all against {against}:")
    base = refs[against]
    lines += distribution(got, base, "hardware (MXFP7 + FP accumulator)")
    for name, arr in refs.items():
        if name == against:
            continue
        lines += distribution(arr, base, name)
    return lines
