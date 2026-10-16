"""Per-item timing for a build against its base, placed and routed.

Reads the image's REPORT_ITEMS and REPORT_BASE from scripts/tcl/<ver>/00_config.tcl
and each build's census (audit/placed_census.tcl, margin 0):

    python scripts/py/timing/items.py v9t5

    build/<design>_placed_classes.txt, build/<design>_routed_classes.txt

for <ver> and its base. Writes build/<ver design>_items.txt (one table per
item) and _items.tsv. Per item and census: TNS, failing endpoints, failing
classes, the worst slack, and that worst class's logic levels and route share
(net / (logic + net)). A class counts in every item whose start and end
regexps both match (Python re.search; empty matches any); `ALL` is every class.
A census that is absent shows "-".
"""

import argparse
import pathlib
import re
import tkinter

ROOT = pathlib.Path(__file__).resolve().parents[3]
TCL = ROOT / "scripts" / "tcl"
ROW = re.compile(
    r"^\s*(-?\d+\.\d+)\s+(\d+)\s+(\d+)\s+(-?\d+\.\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+\|\s+(\S+)\s+->\s+(\S+)\s*$"
)
STAGES = ("placed", "routed")


def tcl_has(t, name):
    return t.eval(f"info exists {name}") == "1"


def config(ver):
    """(design_name, REPORT_BASE, REPORT_ITEMS) of one image."""
    t = tkinter.Tcl()
    t.eval(f"source {{{(TCL / ver / '00_config.tcl').as_posix()}}}")
    base = t.eval("set REPORT_BASE") if tcl_has(t, "REPORT_BASE") else ""
    items = []
    if tcl_has(t, "REPORT_ITEMS"):
        for it in t.splitlist(t.eval("set REPORT_ITEMS")):
            name, start, end = t.splitlist(it)
            items.append((name, re.compile(start), re.compile(end)))
    return t.eval("set design_name"), base, items


def read_classes(path):
    rows = []
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = ROW.match(line)
            if not m:
                continue
            worst, _n, fail, tns, lvl, _dp, logic, net, _clk, start, end = m.groups()
            rows.append(
                {
                    "worst": float(worst),
                    "fail": int(fail),
                    "tns": float(tns),
                    "lvl": int(lvl),
                    "logic": float(logic),
                    "net": float(net),
                    "start": start,
                    "end": end,
                }
            )
    return rows


def stats(rows, start, end):
    """TNS, endpoints, classes, worst, levels, route share; None if no census."""
    if rows is None:
        return None
    sel = [
        r
        for r in rows
        if (start is None or start.search(r["start"]))
        and (end is None or end.search(r["end"]))
    ]
    if not sel:
        return (0.0, 0, 0, None, None, None)
    w = min(sel, key=lambda r: r["worst"])
    share = w["net"] / (w["logic"] + w["net"]) if w["logic"] + w["net"] > 0 else 0.0
    return (
        sum(r["tns"] for r in sel),
        sum(r["fail"] for r in sel),
        len(sel),
        w["worst"],
        w["lvl"],
        share,
    )


def cells(s):
    if s is None:
        return ["-"] * 6
    tns, fail, ncls, worst, lvl, share = s
    return [
        f"{tns:.1f}",
        str(fail),
        str(ncls),
        "-" if worst is None else f"{worst:.3f}",
        "-" if lvl is None else str(lvl),
        "-" if share is None else f"{100 * share:.0f}%",
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ver")
    ap.add_argument("--base", help="the base image (default: its REPORT_BASE)")
    a = ap.parse_args()
    design, base, items = config(a.ver)
    base = a.base or base
    builds = [(a.ver, design)]
    if base:
        builds.insert(0, (base, config(base)[0]))
    census = {
        (v, s): read_classes(ROOT / "build" / f"{d}_{s}_classes.txt")
        for v, d in builds
        for s in STAGES
    }
    # Empty patterns compile to None: they match any class.
    specs = [("ALL", None, None)] + [
        (n, s if s.pattern else None, e if e.pattern else None) for n, s, e in items
    ]
    cols = ["item", "build", "stage", "TNS", "fail", "classes", "worst", "lvl", "route"]
    tsv, txt = ["\t".join(cols)], []
    for name, start, end in specs:
        txt.append(f"== {name}  start /{'' if start is None else start.pattern}/")
        txt.append(f"   end /{'' if end is None else end.pattern}/")
        txt.append(
            f"   {'build':8s} {'stage':7s} {'TNS':>11s} {'fail':>7s} {'classes':>7s} "
            f"{'worst':>7s} {'lvl':>4s} {'route':>6s}"
        )
        for v, _d in builds:
            for s in STAGES:
                c = cells(stats(census[(v, s)], start, end))
                tsv.append("\t".join([name, v, s] + c))
                txt.append(
                    f"   {v:8s} {s:7s} {c[0]:>11s} {c[1]:>7s} {c[2]:>7s} "
                    f"{c[3]:>7s} {c[4]:>4s} {c[5]:>6s}"
                )
        txt.append("")
    out = ROOT / "build" / f"{design}_items"
    out.with_suffix(".tsv").write_text("\n".join(tsv) + "\n", encoding="utf-8")
    out.with_suffix(".txt").write_text("\n".join(txt) + "\n", encoding="utf-8")
    missing = [f"{v} {s}" for (v, s), r in census.items() if r is None]
    print(
        f"{len(specs)} items -> {out}.txt / .tsv; censuses absent: {missing or 'none'}"
    )


if __name__ == "__main__":
    main()
