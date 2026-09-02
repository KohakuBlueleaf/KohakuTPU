"""Per-die resources of a kx_pxache OOC run from its hierarchy report.

    python scripts/py/kx_slr.py build/ooc/kxlive_t0 [build/ooc/kxlive_t1 ...]

A partition is a die. Every depth-1 row of `hier.rpt` belongs to one:
`g_m[p]`, `g_home[p]`, `g_hedge[p]` to die p; a boundary trunk
`g_chain.g_b[b].g_d[d].u_tk` sends from die (d ? b : b+1) and lands on die
(d ? b+1 : b), so its landing rings (`g_ring[*].u_f`, memory and read side)
go to the landing die and the rest (slot mux, credits, TX registers) to the
sending die. The top-level glue row `(kx_pxache)` -- chain heads, inject
arbiters -- is split evenly. Columns: LUT, LUTRAM, FF, RAMB36, RAMB18, URAM.
"""

import argparse
import pathlib
import re
import sys

COLS = ("lut", "lram", "ff", "b36", "b18", "uram")


def parse_rows(path):
    """(depth, instance, module, dict of columns) for every table row."""
    rows = []
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|-"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 11 or not cells[2].isdigit():
            continue
        raw = line.split("|")[1]
        depth = (len(raw) - len(raw.lstrip(" ")) - 1) // 2
        inst = cells[0]
        vals = dict(
            lut=int(cells[2]),
            lram=int(cells[4]),
            ff=int(cells[6]),
            b36=int(cells[7]),
            b18=int(cells[8]),
            uram=int(cells[9]),
        )
        rows.append((depth, inst, cells[1], vals))
    return rows


def add(acc, vals, scale=1.0):
    for c in COLS:
        acc[c] = acc.get(c, 0.0) + vals[c] * scale


def per_die(rows, p):
    dies = [dict() for _ in range(p)]
    total = {}
    idx = 0
    while idx < len(rows):
        depth, inst, _mod, vals = rows[idx]
        if depth == 0:
            add(total, vals)
            idx += 1
            continue
        if depth != 1:
            idx += 1
            continue
        m = re.match(r"(g_m|g_home|g_hedge)\[(\d+)\]", inst)
        t = re.match(r"g_chain\.g_b\[(\d+)\]\.g_d\[(\d+)\]\.u_tk", inst)
        if m:
            add(dies[int(m.group(2))], vals)
        elif t:
            b, d = int(t.group(1)), int(t.group(2))
            send, land = (b, b + 1) if d else (b + 1, b)
            rings = {}
            j = idx + 1
            while j < len(rows) and rows[j][0] > 1:
                if rows[j][0] == 2 and re.match(r"g_ring\[\d+\]\.u_f", rows[j][1]):
                    add(rings, rows[j][3])
                j += 1
            add(dies[land], rings)
            rest = {c: vals[c] - rings.get(c, 0.0) for c in COLS}
            add(dies[send], rest)
        elif inst.startswith("("):
            for die in dies:
                add(die, vals, 1.0 / p)
        elif re.match(r"g_stn\[(\d+)\]", inst):
            # the station bus: station s is die s; a link's two halves
            # straddle the boundary, half to each die
            add(dies[int(re.match(r"g_stn\[(\d+)\]", inst).group(1))], vals)
        elif re.match(r"g_link\[(\d+)\]", inst):
            b = int(re.match(r"g_link\[(\d+)\]", inst).group(1))
            add(dies[b], vals, 0.5)
            add(dies[b + 1], vals, 0.5)
        else:
            # a row the map does not know: split evenly, and say so
            print(f"  ? {inst}: split evenly", file=sys.stderr)
            for die in dies:
                add(die, vals, 1.0 / p)
        idx += 1
    return dies, total


def fmt(v):
    return f"{v:,.0f}" if abs(v - round(v)) < 1e-6 else f"{v:,.1f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--p", type=int, default=4, help="partitions (dies)")
    a = ap.parse_args()
    print("| run | die | LUT | LUTRAM | FF | RAMB36 | RAMB18 | tiles | URAM |")
    print("|---|---|---|---|---|---|---|---|---|")
    for run in a.runs:
        rows = parse_rows(pathlib.Path(run) / "hier.rpt")
        dies, total = per_die(rows, a.p)
        name = pathlib.Path(run).name
        for i, die in enumerate(dies):
            tiles = die["b36"] + die["b18"] / 2
            print(
                f"| {name} | {i} | {fmt(die['lut'])} | {fmt(die['lram'])} | "
                f"{fmt(die['ff'])} | {fmt(die['b36'])} | {fmt(die['b18'])} | "
                f"{fmt(tiles)} | {fmt(die['uram'])} |"
            )
        tiles = total["b36"] + total["b18"] / 2
        print(
            f"| {name} | all | {fmt(total['lut'])} | {fmt(total['lram'])} | "
            f"{fmt(total['ff'])} | {fmt(total['b36'])} | {fmt(total['b18'])} | "
            f"{fmt(tiles)} | {fmt(total['uram'])} |"
        )


if __name__ == "__main__":
    main()
