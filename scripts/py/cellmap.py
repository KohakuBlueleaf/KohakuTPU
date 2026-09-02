"""Physical occupancy from a routed run's cell_loc.tsv.

The device is read as a grid of CLB columns (SLICE_X) and rows (SLICE_Y):
xcvu13p is 4 SLRs of 240 rows each, 16 clock regions of 60 rows per SLR
column band. Reports, for each named group of cells, its column and row
span, the rows/columns it actually occupies, and the per-clock-region
count -- the numbers that say whether a module sits in a block or is
smeared across the die.

    python scripts/py/cellmap.py build/impl_quad_q1 --group "g_n\\[(\\d)\\]"
"""

import argparse
import collections
import pathlib
import re

SLR_ROWS = 240  # xcvu13p: 4 SLRs, SLICE_Y 0..959
CR_ROWS = 60  # clock region height in CLB rows
CR_COLS = 36  # clock region width in SLICE columns (approx, X0..X287 / 8)

LOC_RE = re.compile(r"^(SLICE|RAMB36|RAMB18|DSP48E2|URAM288)_X(\d+)Y(\d+)$")


def read_locs(path):
    """[(kind, x, y, name)] for every placed cell in the dump."""
    out = []
    with open(path, encoding="utf-8", errors="ignore") as fh:
        head = fh.readline()
        if not head.startswith("LOC"):
            fh.seek(0)
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            m = LOC_RE.match(parts[0])
            if not m:
                continue
            out.append((m.group(1), int(m.group(2)), int(m.group(3)), parts[1]))
    return out


def group_of(name, pats):
    for label, rx in pats:
        m = rx.search(name)
        if m:
            return label if not m.groups() else f"{label}{'/'.join(m.groups())}"
    return None


def summarise(cells, label):
    xs = [c[1] for c in cells]
    ys = [c[2] for c in cells]
    cols = collections.Counter(xs)
    rows = collections.Counter(ys)
    slrs = collections.Counter(y // SLR_ROWS for y in ys)
    crs = collections.Counter((y // CR_ROWS, x // CR_COLS) for _, x, y, _ in cells)
    kinds = collections.Counter(c[0] for c in cells)
    span_x = max(xs) - min(xs) + 1
    span_y = max(ys) - min(ys) + 1
    return {
        "label": label,
        "cells": len(cells),
        "x_lo": min(xs),
        "x_hi": max(xs),
        "y_lo": min(ys),
        "y_hi": max(ys),
        "span_x": span_x,
        "span_y": span_y,
        "cols_used": len(cols),
        "rows_used": len(rows),
        "col_fill": len(cols) / span_x,
        "row_fill": len(rows) / span_y,
        "slrs": sorted(slrs.items()),
        "crs": len(crs),
        "top_cr": crs.most_common(4),
        "kinds": kinds,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rundir")
    ap.add_argument(
        "--group",
        action="append",
        default=[],
        help="LABEL=REGEX; a capture group is appended to the label",
    )
    ap.add_argument("--top", type=int, default=12, help="busiest columns to list")
    args = ap.parse_args()

    run = pathlib.Path(args.rundir)
    cells = read_locs(run / "cell_loc.tsv")
    print(f"{run.name}: {len(cells)} placed cells")

    pats = []
    for spec in args.group:
        label, _, rx = spec.partition("=")
        pats.append((label, re.compile(rx if rx else label)))
    if not pats:
        pats = [("all", re.compile(""))]

    buckets = collections.defaultdict(list)
    for c in cells:
        g = group_of(c[3], pats)
        if g is not None:
            buckets[g].append(c)

    hdr = (
        f"{'group':<14}{'cells':>8}{'X span':>12}{'Y span':>12}"
        f"{'cols':>7}{'rows':>7}{'colfill':>9}{'rowfill':>9}{'SLRs':>18}{'CRs':>5}"
    )
    print(hdr)
    print("-" * len(hdr))
    for g in sorted(buckets):
        s = summarise(buckets[g], g)
        slr = ",".join(f"{k}:{v}" for k, v in s["slrs"])
        print(
            f"{s['label']:<14}{s['cells']:>8}"
            f"{s['x_lo']:>6}-{s['x_hi']:<6}{s['y_lo']:>6}-{s['y_hi']:<6}"
            f"{s['cols_used']:>7}{s['rows_used']:>7}"
            f"{s['col_fill']:>9.2f}{s['row_fill']:>9.2f}{slr:>18}{s['crs']:>5}"
        )

    print()
    print("busiest CLB columns (SLICE_X: cells)")
    cols = collections.Counter(x for k, x, _, _ in cells if k == "SLICE")
    for x, n in cols.most_common(args.top):
        print(f"  X{x:<5} {n}")
    rows = collections.Counter(y for k, _, y, _ in cells if k == "SLICE")
    print("busiest CLB rows (SLICE_Y: cells)")
    for y, n in rows.most_common(args.top):
        print(f"  Y{y:<5} {n}  (SLR {y // SLR_ROWS})")


if __name__ == "__main__":
    main()
