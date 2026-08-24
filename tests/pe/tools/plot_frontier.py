"""Render the PE's LUT-vs-Fmax frontier from the points CSV.

    python tests/pe/tools/plot_frontier.py --csv <file> --out <dir>

Two figures:

  frontier.png   LUT against ACHIEVED Fmax. A variant's targets are joined into
                 one run and labelled once at its relaxed end; the Pareto set is
                 a staircase, the dominated region shaded, the baseline ringed.
  targets.png    the same points read the other way -- per variant, one
                 connected series across clock targets, because LUT is not
                 independent of the constraint and a variant is a curve.

In the scatter colour carries the ARM and nothing else -- three categorical
slots, which is what the palette validates for an all-pairs form -- and the
connected run plus its label separate variants within an arm. In the panels
colour carries the CONFIG, five slots, assigned by name so that adding a variant
never repaints one already drawn; the baseline is a dashed reference there
rather than a sixth series. Identity is never colour alone in either.

Both paths are REQUIRED arguments. This file is tracked and where the research
lives is not its business.
"""

import argparse
import csv
import pathlib
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"

# Categorical slots 1-3, validated all-pairs on this surface.
ARM_COLOR = {"-": "#2a78d6", "A": "#eb6834", "B": "#1baf7a"}
ARM_NAME = {"-": "baseline", "A": "arm A — low LUT", "B": "arm B — high Fmax"}
# Slots 1-5 for the per-variant series, validated adjacent.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]

# COLOUR FOLLOWS THE CONFIG, NEVER ITS RANK in whatever subset is being drawn:
# adding a variant must not repaint the ones already plotted, so entries are
# APPENDED here and never reordered.
#
# Five validated slots. A sixth series folds to neutral rather than cycling a
# hue, which is why order matters: whatever sits at index 5 loses its colour and
# carries its direct label alone. It stays distinguishable from the baseline --
# also neutral -- because variants are solid with markers and the baseline is
# dashed without them.
ARM_CONFIGS = {
    "A": ("a-nofwd", "a-btb0", "a-nofwd0", "a-l164", "a-min", "a-l164b0"),
    "B": ("b-bram-rf", "b-btb64"),
}
BASE_CONFIG = "balanced"


def series_color(arm, name, extras):
    known = ARM_CONFIGS.get(arm, ())
    i = known.index(name) if name in known else len(known) + extras.index(name)
    return SERIES[i] if i < len(SERIES) else MUTED


plt.rcParams.update(
    {
        "font.family": "sans-serif",
        "font.sans-serif": ["Segoe UI", "DejaVu Sans"],
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "text.color": INK,
        "axes.labelcolor": INK2,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
    }
)


def load(path):
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("fmax_mhz")]
    for r in rows:
        r["fmax"] = float(r["fmax_mhz"])
        r["lutn"] = int(r["lut"])
        r["tgt"] = float(r["target_ns"])
        r["rtl"] = r.get("rtl") or "unversioned"
    return rows


def revisions(rows):
    """Oldest first, by the order they first appear in the append-only file.
    Nothing else in the CSV records time, so row order IS the chronology."""
    seen = []
    for r in rows:
        if r["rtl"] not in seen:
            seen.append(r["rtl"])
    return seen


def staircase(front):
    """To beat a frontier point you must move up and left, so the boundary
    between dominated and not is a staircase rather than a line through them."""
    sx, sy = [], []
    for i, r in enumerate(front):
        sx.append(r["fmax"])
        sy.append(r["lutn"])
        if i + 1 < len(front):
            sx.append(front[i + 1]["fmax"])
            sy.append(r["lutn"])
    return sx, sy


def pareto(rows):
    """Low LUT and high Fmax both win, so a point survives only if nothing else
    is at least as fast AND no larger."""
    keep = []
    for r in rows:
        if not any(
            (
                o["fmax"] >= r["fmax"]
                and o["lutn"] <= r["lutn"]
                and (o["fmax"] > r["fmax"] or o["lutn"] < r["lutn"])
            )
            for o in rows
        ):
            keep.append(r)
    return sorted(keep, key=lambda r: r["fmax"])


def dress(ax):
    ax.set_axisbelow(True)
    ax.grid(True, color=GRID, linewidth=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(1.0)


def fig_frontier(rows, out):
    fig, ax = plt.subplots(figsize=(11, 7))
    dress(ax)

    # ONE HULL PER RTL REVISION. A configuration is a parameter set and shares
    # the RTL; an RTL change moves every point at once, so the revisions have
    # separate frontiers and the movement between them is the result. The newest
    # is the solid one that shades its dominated region; older ones stay as thin
    # dashed reference hulls so the figure reads as "where it was, where it is".
    revs = revisions(rows)
    top = max(r["lutn"] for r in rows) * 1.04
    for i, rev in enumerate(revs):
        front = pareto([r for r in rows if r["rtl"] == rev])
        if len(front) < 2:
            continue
        sx, sy = staircase(front)
        if rev == revs[-1]:
            ax.plot(sx, sy, color=MUTED, linewidth=2, zorder=2)
            ax.fill_between(sx, sy, top, color=MUTED, alpha=0.07, zorder=1)
        else:
            ax.plot(
                sx,
                sy,
                color=MUTED,
                linewidth=1.2,
                linestyle=(0, (5, 3)),
                alpha=0.65,
                zorder=2,
            )
        if len(revs) > 1:
            ax.annotate(
                rev,
                (sx[0], sy[0]),
                textcoords="offset points",
                xytext=(-6, -4),
                fontsize=9,
                color=MUTED,
                ha="right",
                va="top",
                weight="bold" if rev == revs[-1] else "normal",
            )
    front = pareto([r for r in rows if r["rtl"] == revs[-1]])

    # A variant is a CURVE here as much as in the target view: its five targets
    # trace a run up and to the right. Joining them lets one label name the whole
    # run, instead of a label on every point, which at 30 points is unreadable.
    # A run's relaxed end is very often ON the frontier, and it would then carry
    # both its own name and the Pareto label on top of each other.
    labelled = {(r["config"], r["target_ns"]) for r in front}

    # A run is (config x revision), never config alone: the same config measured
    # on two revisions is two runs, and joining them draws a line between designs.
    for rev in revs:
        cur = rev == revs[-1]
        for arm in ("-", "A", "B"):
            for name in sorted(
                {r["config"] for r in rows if r["arm"] == arm and r["rtl"] == rev}
            ):
                pts = sorted(
                    (r for r in rows if r["config"] == name and r["rtl"] == rev),
                    key=lambda r: r["tgt"],
                    reverse=True,
                )
                ax.plot(
                    [p["fmax"] for p in pts],
                    [p["lutn"] for p in pts],
                    color=ARM_COLOR[arm],
                    linewidth=1.2,
                    alpha=0.55 if cur else 0.25,
                    zorder=3,
                )
                ax.scatter(
                    [p["fmax"] for p in pts],
                    [p["lutn"] for p in pts],
                    s=90 if cur else 40,
                    zorder=4 if cur else 3,
                    color=ARM_COLOR[arm] if cur else "none",
                    edgecolors=SURFACE if cur else ARM_COLOR[arm],
                    linewidths=2 if cur else 1.2,
                    alpha=1.0 if cur else 0.55,
                )
                if not cur:
                    continue
                if (pts[0]["config"], pts[0]["target_ns"]) in labelled:
                    continue
                # At the relaxed end, which is the bottom-left of the run.
                ax.annotate(
                    name,
                    (pts[0]["fmax"], pts[0]["lutn"]),
                    textcoords="offset points",
                    xytext=(-9, -6),
                    fontsize=9,
                    color=INK2,
                    ha="right",
                    va="top",
                )

    base = [
        r
        for r in rows
        if r["config"] == BASE_CONFIG and r["tgt"] == 2.5 and r["rtl"] == revs[-1]
    ]
    if base:
        ax.scatter(
            [base[0]["fmax"]],
            [base[0]["lutn"]],
            s=320,
            facecolors="none",
            edgecolors=INK,
            linewidths=2,
            zorder=5,
        )
        ax.annotate(
            "accepted baseline",
            (base[0]["fmax"], base[0]["lutn"]),
            textcoords="offset points",
            xytext=(0, 26),
            ha="center",
            fontsize=10,
            color=INK,
            weight="bold",
        )

    # Only the Pareto points carry their target: those are the ones anybody has
    # to be able to reproduce from the figure alone.
    for r in pareto(rows):
        ax.annotate(
            "%s @ %s ns" % (r["config"], r["target_ns"]),
            (r["fmax"], r["lutn"]),
            textcoords="offset points",
            xytext=(8, -12),
            fontsize=9,
            color=INK,
            weight="bold",
        )

    # Labels sit to the right of their point, so the data needs room past the
    # last one or the longest name runs off the axis.
    ax.margins(x=0.16, y=0.10)
    ax.set_xlabel("achieved Fmax (MHz, OOC synth-only)")
    ax.set_ylabel("LUT (CLB LUT sites)")
    ax.set_title(
        "Controller PE — LUT against achieved frequency",
        fontsize=15,
        weight="bold",
        color=INK,
        loc="left",
        pad=16,
    )
    ax.text(
        0,
        1.015,
        "each point is one configuration at one clock target; "
        "the staircase is the Pareto set, shaded region is dominated",
        transform=ax.transAxes,
        fontsize=10,
        color=INK2,
    )

    ax.legend(
        handles=[
            Line2D(
                [],
                [],
                marker="o",
                linestyle="none",
                markersize=9,
                markerfacecolor=ARM_COLOR[a],
                markeredgecolor=SURFACE,
                label=ARM_NAME[a],
            )
            for a in ("-", "A", "B")
            if any(r["arm"] == a for r in rows)
        ],
        frameon=False,
        loc="upper left",
        fontsize=10,
    )

    fig.tight_layout()
    fig.savefig(out / "frontier.png", dpi=160)
    plt.close(fig)


def fig_targets(rows, out):
    # CURRENT REVISION ONLY. This figure reads a variant as a curve through the
    # clock targets; overlaying two designs' curves on one axis would compare
    # things that were never alternatives to each other.
    rows = [r for r in rows if r["rtl"] == revisions(rows)[-1]]
    arms = [a for a in ("A", "B") if any(r["arm"] == a for r in rows)] or ["-"]
    fig, axes = plt.subplots(
        2, len(arms), figsize=(6.2 * len(arms), 8.4), squeeze=False
    )

    for col, arm in enumerate(arms):
        names = sorted(
            {
                r["config"]
                for r in rows
                if r["arm"] == arm and r["config"] != BASE_CONFIG
            }
        )
        extras = [n for n in names if n not in ARM_CONFIGS.get(arm, ())]
        for row, (key, label) in enumerate(
            [("lutn", "LUT (CLB LUT sites)"), ("fmax", "achieved Fmax (MHz)")]
        ):
            ax = axes[row][col]
            dress(ax)
            # Labels are collected and placed together below: every series is
            # anchored at the same relaxed-end x and several share a value to
            # the LUT, so any per-series offset rule eventually collides.
            tags = []
            # The baseline is the thing to beat, not a peer series: it rides in
            # every panel as a dashed reference and spends no categorical slot.
            base = sorted(
                (r for r in rows if r["config"] == BASE_CONFIG), key=lambda r: r["tgt"]
            )
            if base:
                ax.plot(
                    [p["tgt"] for p in base],
                    [p[key] for p in base],
                    linestyle="--",
                    linewidth=2,
                    color=INK2,
                    zorder=2,
                )
                tags.append((base[-1][key], BASE_CONFIG, INK2, "bold"))
            anchor = base[-1]["tgt"] if base else None
            for name in names:
                pts = sorted(
                    (r for r in rows if r["config"] == name), key=lambda r: r["tgt"]
                )
                colour = series_color(arm, name, extras)
                ax.plot(
                    [p["tgt"] for p in pts],
                    [p[key] for p in pts],
                    marker="o",
                    markersize=8,
                    linewidth=2,
                    color=colour,
                    markeredgecolor=SURFACE,
                    markeredgewidth=1.5,
                    zorder=3,
                )
                anchor = pts[-1]["tgt"]
                tags.append((pts[-1][key], name, colour, "normal"))

            # Ordered by value and stepped a fixed distance apart, so the label
            # column reads in the same order as the curves it names and no two
            # can land on each other however close their values are.
            for rank, (yv, name, colour, wt) in enumerate(sorted(tags)):
                ax.annotate(
                    name,
                    (anchor, yv),
                    textcoords="offset points",
                    xytext=(4, 6 + 14 * rank),
                    fontsize=9,
                    color=colour,
                    weight=wt,
                    ha="left",
                )
            ax.margins(0.14)
            ax.invert_xaxis()
            ax.set_xlabel("clock target (ns, tighter to the right)")
            ax.set_ylabel(label)
            if row == 0:
                ax.set_title(
                    ARM_NAME[arm],
                    fontsize=13,
                    weight="bold",
                    color=INK,
                    loc="left",
                    pad=10,
                )

    fig.suptitle(
        "Controller PE — what the clock constraint costs",
        fontsize=15,
        weight="bold",
        color=INK,
        x=0.01,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out / "targets.png", dpi=160)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    rows = load(a.csv)
    if not rows:
        print("no measured points in %s" % a.csv)
        return 1
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    fig_frontier(rows, out)
    fig_targets(rows, out)
    print("  %d points -> %s" % (len(rows), out))
    for rev in revisions(rows):
        sub = [r for r in rows if r["rtl"] == rev]
        print("  rtl %s (%d points)" % (rev, len(sub)))
        for r in pareto(sub):
            print(
                "    pareto: %-10s @ %-6s %6.1f MHz  %5d LUT"
                % (r["config"], r["target_ns"], r["fmax"], r["lutn"])
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
