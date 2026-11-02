"""Removable work by type: each run's step against its serial floors, what each detector
would recover, and the form chains standing between computations."""

import collections
import json

import matplotlib.pyplot as plt
from runs import load, names
from style import DATA, MUTED, SECOND, SERIES, save, stacked_bars

DETECTORS = {
    "form_held": "form already held",
    "written_unread": "written, never read",
    "launch_seam": "seam between launches",
}


def reports() -> dict:
    """Each run's detectors, keyed with its board and nodes. Detector cycles are the
    timed model's, over the model's own step; `card_cycles` is the card's step."""
    out = {}
    for name in names():
        path = DATA / name / "waste.json"
        if path.exists():
            report = json.loads(path.read_text("utf-8"))
            report["card_cycles"] = sum(r["cycles"] for r in load(name).step)
            out[name.replace(".multimesh_", " ")] = report
    return out


def ladder(runs: dict) -> None:
    order = sorted(runs, key=lambda n: runs[n]["card_cycles"])
    parts: dict[str, list[float]] = {
        "math floor": [],
        "memory floor over math": [],
        "form work floor": [],
        "over the floors": [],
    }
    for n in order:
        r, f = runs[n], runs[n]["floors"]
        parts["math floor"].append(f["math"] / 1e6)
        parts["memory floor over math"].append((f["compute"] - f["math"]) / 1e6)
        parts["form work floor"].append((f["with_form"] - f["compute"]) / 1e6)
        parts["over the floors"].append(max(0, r["card_cycles"] - f["with_form"]) / 1e6)
    colors = dict(zip(parts, (SERIES[2], SERIES[3], SERIES[0], MUTED), strict=True))
    fig, ax = plt.subplots(figsize=(12, 0.45 * len(order) + 1.6))
    stacked_bars(ax, order, parts, colors)
    for i, n in enumerate(order):
        r = runs[n]
        share = r["floors"]["math"] / r["card_cycles"]
        ax.text(
            r["card_cycles"] / 1e6,
            i,
            f" {r['card_cycles'] / 1e6:.1f} M, math {share:.1%}",
            va="center",
            color=SECOND,
            fontsize=8,
        )
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node cycles a warm step (M)")
    ax.legend(ncol=4, loc="upper center", bbox_to_anchor=(0.5, 1.13))
    ax.set_title("A step against its serial floors", pad=26)
    save(fig, "waste", "ladder")


def recoverable(runs: dict) -> None:
    order = sorted(runs)
    parts = {
        title: [
            runs[n]["recoverable"].get(key, 0.0) / runs[n]["step_cycles"] for n in order
        ]
        for key, title in DETECTORS.items()
    }
    colors = dict(zip(parts, (SERIES[0], SERIES[1], SERIES[2]), strict=True))
    fig, ax = plt.subplots(figsize=(11, 0.45 * len(order) + 1.6))
    stacked_bars(ax, order, parts, colors)
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.1%}")
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("share of the timed model's warm step a detector names")
    ax.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.13))
    ax.set_title("What the detectors would recover, in model cycles", pad=26)
    save(fig, "waste", "recoverable")


def chains(runs: dict) -> None:
    """The form chains that cost most: one row a pattern, cycles over all runs."""
    total = collections.Counter()
    by_run = collections.defaultdict(collections.Counter)
    for n, r in runs.items():
        for c in r["chains"]:
            key = " ".join(c["kinds"])
            key = key if len(key) < 60 else key[:57] + "..."
            share = c["cycles"] / r["step_cycles"]
            total[key] += share
            by_run[n][key] += share
    rows = [k for k, _ in total.most_common(16)][::-1]
    order = sorted(by_run, key=lambda n: -sum(by_run[n].values()))[:8]
    parts = {n: [by_run[n][k] for k in rows] for n in order}
    colors = dict(zip(order, SERIES, strict=False))
    fig, ax = plt.subplots(figsize=(13, 0.4 * len(rows) + 1.6))
    stacked_bars(ax, rows, parts, colors)
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.1%}")
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("share of a modelled warm step, summed over runs")
    ax.legend(ncol=4, loc="lower right")
    ax.set_title("Form chains between two computations")
    save(fig, "waste", "chains")


def main():
    runs = reports()
    ladder(runs)
    recoverable(runs)
    chains(runs)


if __name__ == "__main__":
    main()
