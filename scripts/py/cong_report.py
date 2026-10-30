#!/usr/bin/env python3
"""Roll up what 86_congestion.tcl exported into tables a person can read.

    python scripts/py/cong_report.py multimesh_v8t3 [--depth 2] [--top 14]

Four tables per design: who sits inside each congestion window, who owns the
nets that end inside it, who owns the nets that only pass through it, where
each module's cells actually sit (clock region span), and which columns the SLR
crossings use. Owners come out of the Tcl three levels deep; --depth trims them
so the roll-up is readable.
"""

import argparse
import collections
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
BUILD = ROOT / "build"


def rows(path):
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        return []
    head = lines[0].split("\t")
    out = []
    for ln in lines[1:]:
        f = ln.split("\t")
        if len(f) == len(head):
            out.append(dict(zip(head, f)))
    return out


def trim(owner, depth):
    return "/".join(owner.split("/")[:depth])


def table(title, pairs, total=None, top=14):
    print(f"\n--- {title}")
    if not pairs:
        print("  (nothing)")
        return
    if total is None:
        total = sum(v for _, v in pairs)
    for k, v in sorted(pairs, key=lambda kv: -kv[1])[:top]:
        pct = (100.0 * v / total) if total else 0.0
        print(f"  {v:9,d}  {pct:5.1f}%  {k}")
    shown = sum(v for _, v in sorted(pairs, key=lambda kv: -kv[1])[:top])
    if total > shown:
        print(
            f"  {total - shown:9,d}  {100.0 * (total - shown) / total:5.1f}%  (the rest)"
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("design")
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--top", type=int, default=14)
    a = ap.parse_args()
    d = a.design

    wins = rows(BUILD / f"{d}_cong_windows.tsv")
    cells = rows(BUILD / f"{d}_cong_cells.tsv")
    nets = rows(BUILD / f"{d}_cong_nets.tsv")

    pairs = rows(BUILD / f"{d}_cong_pairs.tsv")

    print(f"=== {d}: congestion windows ({len(wins)})")
    for w in wins:
        thru = int(w.get("NETS_THROUGH", 0) or 0)
        routed = int(w.get("NETS_ROUTED", 0) or 0)
        pct = (100.0 * thru / routed) if routed else 0.0
        print(
            f"  {w['SECTION'][:34]:34s} {w['DIRECTION']:6s} {w['TYPE']:7s} L{w['LEVEL']}"
            f"  cols {w['COL0']}-{w['COL1']} rows {w['ROW0']}-{w['ROW1']}"
            f"  cells {int(w['CELLS']):,}  routed nets {routed:,}"
            f"  pass-through {thru:,} ({pct:.0f}%)"
        )

    by_win_cells = collections.defaultdict(collections.Counter)
    for r in cells:
        by_win_cells[r["WINDOW"]][trim(r["OWNER"], a.depth)] += int(r["CELLS"])
    by_win_nets = collections.defaultdict(
        lambda: collections.defaultdict(collections.Counter)
    )
    for r in nets:
        if "OWNER" not in r:
            continue
        by_win_nets[r["WINDOW"]][r["KIND"]][trim(r["OWNER"], a.depth)] += int(r["NETS"])
    by_win_pairs = collections.defaultdict(collections.Counter)
    for r in pairs:
        src = trim(r["SRC_OWNER"], a.depth)
        dst = trim(r["DST_OWNER"], a.depth)
        by_win_pairs[r["WINDOW"]][f"{src}  ->  {dst}"] += int(r["NETS"])

    for win in by_win_cells:
        print(f"\n=== window {win}")
        table(
            "cells placed inside, by owner", list(by_win_cells[win].items()), top=a.top
        )
        for kind in ("ENDPOINT", "PASS_THROUGH"):
            table(
                f"nets {kind.lower().replace('_', ' ')} the window, by owning scope",
                list(by_win_nets[win][kind].items()),
                top=a.top,
            )
        table(
            "pass-through nets, driver -> load (sampled)",
            list(by_win_pairs[win].items()),
            top=a.top,
        )

    # ---- footprint: which clock regions each module actually occupies --------
    reg = rows(BUILD / f"{d}_cell_regions_top.tsv")
    if reg:
        per = collections.defaultdict(collections.Counter)
        for r in reg:
            per[r["MODULE"]][r["CLOCK_REGION"]] += int(r["CELLS"])
        print("\n=== module footprint (clock regions; SLR = region row // 4)")
        print(f"  {'module':28s} {'cells':>9s}  {'regions':>7s}  cells per SLR (0..3)")
        for mod, c in sorted(per.items(), key=lambda kv: -sum(kv[1].values())):
            tot = sum(c.values())
            if tot < 64:
                continue
            slr = [0, 0, 0, 0]
            for cr, n in c.items():
                if cr.startswith("X") and "Y" in cr:
                    try:
                        y = int(cr.split("Y")[1])
                    except ValueError:
                        continue
                    slr[min(3, y // 4)] += n
            print(
                f"  {mod:28s} {tot:9,d}  {len(c):7d}  "
                + " ".join(f"{v:8,d}" for v in slr)
            )

        print(
            "\n  SLL usage per boundary and per owner is in"
            f" {d}_impl_util_slr.rpt and {d}_impl_congestion.rpt."
        )


if __name__ == "__main__":
    main()
