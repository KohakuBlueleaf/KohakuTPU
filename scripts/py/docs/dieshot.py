"""A die shot of a placed or routed checkpoint, from scripts/tcl/audit/dieshot.tcl's dump.

    python scripts/py/docs/dieshot.py DUMP_DIR OUT.png [--title TEXT]

Every fabric site (slice, RAMB36, DSP, URAM) is one cell of a column x
slice-row grid and takes the colour of the block that holds most of its
leaves; empty sites show their type. Clock regions are drawn square and the
device is turned 90 degrees, so the dies sit in one row as they abut (die 0 on
the left), with the legend beside them.
"""

import argparse
import bisect
import re
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

#: (label, colour, regexp over the cell path below the block design instance)
BLOCKS = [
    ("matmul cluster", "#3d7eff", r"^mesh_\d+/inst/u_cu"),
    ("vector core", "#18b04a", r"^mesh_\d+/inst/u_vec"),
    ("system node", "#ff9a1f", r"^mesh_\d+/inst/u_mag"),
    ("NoC routers and links", "#e8e05a", r"^mesh_\d+/"),
    ("Xache", "#ff4fd8", r"^xache/"),
    ("DDR4 controller", "#b58bff", r"^(ddr4_|rst_ddr4_)"),
    ("interlink", "#2fd3e0", r"^pipe_"),
    ("station bus and host link", "#ff4b4b", r"^(station_bus|xdma_|jtag_|dwc_|rs_)"),
    ("clocking, resets, other", "#9a9a9a", r""),
]
EMPTY = {
    "SLICEL": "#1b1b1b",
    "SLICEM": "#232323",
    "RAMB36": "#14274f",
    "DSP48E2": "#103f3a",
    "URAM288": "#3b1748",
}
BG = "#060606"
CW = 3


def rows_of(path: Path) -> list:
    return [ln.split() for ln in path.read_text().split("\n") if ln]


def rgb(hexstr: str) -> list:
    return [int(hexstr[i : i + 2], 16) for i in (1, 3, 5)]


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("dump", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--title", default="")
    a = ap.parse_args()

    # Site kinds by site name (a RAMB36 site's SITE_TYPE reads RAMBFIFO36).
    kind = {
        "SLICE": None,
        "RAMB36": "RAMB36",
        "DSP48E2": "DSP48E2",
        "URAM288": "URAM288",
    }
    sites = {}
    for n, t, x, y in rows_of(a.dump / "bg.txt"):
        k = kind[n.split("_X")[0]] or t
        sites[n] = (k, int(x), int(y))
    cols = sorted({x for _, x, _ in sites.values()})
    srows = sorted({y for t, _, y in sites.values() if t.startswith("SLICE")})
    flip = sites["SLICE_X0Y0"][2] > sites["SLICE_X0Y1"][2]
    step = int(np.median(np.diff(srows)))

    def gx(x: int) -> int:
        return bisect.bisect_left(cols, x) * CW

    def gy(y: int) -> int:
        r = bisect.bisect_left(srows, y) if flip else bisect.bisect_right(srows, y) - 1
        return max(r, 0)

    # A site's height in slice rows, from its kind's RPM_Y pitch in one column.
    per_col = defaultdict(list)
    for t, x, y in sites.values():
        per_col[(t, x)].append(y)
    span = {}
    for t in EMPTY:
        d = [
            np.diff(sorted(v))
            for (tt, _), v in per_col.items()
            if tt == t and len(v) > 1
        ]
        span[t] = max(1, round(int(np.median(np.concatenate(d))) / step)) if d else 1

    pats = [(lab, re.compile(p)) for lab, _, p in BLOCKS]
    occ = defaultdict(lambda: defaultdict(int))
    for key, site, n in rows_of(a.dump / "cells_hier.txt"):
        path = key.split("/", 1)[1] if "/" in key else key
        lab = next(lab for lab, p in pats if p.search(path))
        occ[site][lab] += int(n)

    w, h = len(cols) * CW, len(srows)
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = rgb(BG)
    colour = {lab: rgb(c) for lab, c, _ in BLOCKS}
    for n, (t, x, y) in sites.items():
        c = (
            colour[max(occ[n].items(), key=lambda kv: kv[1])[0]]
            if n in occ
            else rgb(EMPTY[t])
        )
        r0, x0 = gy(y), gx(x)
        img[r0 : r0 + span[t], x0 : x0 + CW - 1] = c

    # Clock-region outlines into the raster; each die's slice-row band.
    slr_rows = defaultdict(list)
    cr_cols = set()
    cr_h = 0
    for name, slr, x0, x1, y0, y1 in rows_of(a.dump / "cr.txt"):
        r0, r1 = sorted((gy(int(y0)), gy(int(y1))))
        c0, c1 = gx(int(x0)), min(gx(int(x1)) + CW, w) - 1
        slr_rows[int(slr)] += [r0, r1]
        cr_cols.add(name.split("Y")[0])
        cr_h = max(cr_h, r1 - r0 + 1)
        img[r0, c0 : c1 + 1] = img[r1, c0 : c1 + 1] = rgb("#4a4a4a")
        img[r0 : r1 + 1, c0] = img[r0 : r1 + 1, c1] = rgb("#4a4a4a")

    # Square clock regions, then rotated so the dies sit in one row: die 0 on
    # the left, column X0 at the top.
    width = len(cr_cols) * cr_h
    img = img[:, (np.arange(width) * w // width)]
    img = img.transpose(1, 0, 2)
    for d, rs in slr_rows.items():
        if d > 0:
            img[:, min(rs)] = rgb("#ffe14d")
    scale = 2
    img = img.repeat(scale, 0).repeat(scale, 1)
    ih, iw = img.shape[:2]

    dpi, legend_w = 100, 2.6
    fig = plt.figure(
        figsize=(iw / dpi + legend_w, ih / dpi + 0.1), dpi=dpi, facecolor=BG
    )
    fw, fh = fig.get_size_inches()
    ax = fig.add_axes([0, 0.05 / fh, iw / dpi / fw, ih / dpi / fh])
    ax.imshow(img, interpolation="nearest")
    ax.set_axis_off()
    for d, rs in sorted(slr_rows.items()):
        ax.text(
            (min(rs) + 4) * scale,
            14,
            f"die {d}",
            color="#ffffff",
            fontsize=11,
            va="top",
            fontweight="bold",
        )

    handles = [mpatches.Patch(color=c, label=lab) for lab, c, _ in BLOCKS]
    handles += [mpatches.Patch(color=c, label=f"empty {t}") for t, c in EMPTY.items()]
    fig.legend(
        handles=handles,
        loc="center right",
        fontsize=9,
        facecolor="#141414",
        labelcolor="#e6e6e6",
        edgecolor="#444444",
    )
    if a.title:
        fig.text(
            1 - legend_w / fw + 0.01,
            0.03,
            a.title,
            color="#cccccc",
            fontsize=7,
            wrap=True,
        )
    fig.savefig(a.out, facecolor=BG)
    print(f"{a.out}: {fw * dpi:.0f} x {fh * dpi:.0f} px")


if __name__ == "__main__":
    main()
