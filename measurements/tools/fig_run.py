"""Every figure of one run: where a warm step's time goes, seen from the card."""

import sys

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch
from runs import by_kind, load
from style import KINDS, MUTED, PHASES, SECOND, SURFACE, save, stacked_bars


def unit_lanes(step: list[dict]) -> list[tuple]:
    lanes = sorted({(u["kind"], u["x"], u["y"]) for r in step for u in r["units"]})
    return lanes


def gantt(run, out: str) -> None:
    """A warm step as a machine would see it: a lane for the run and a lane per unit;
    blank space is time nothing was doing."""
    step = run.step
    t0 = step[0]["start"]
    lanes = unit_lanes(step)
    names = ["launch", *[f"{k} ({x},{y})" for k, x, y in lanes]]
    fig, ax = plt.subplots(figsize=(16, 1.1 + 0.62 * len(names)))
    for r in step:
        at = (r["start"] - t0) / 1e6
        wide = r["cycles"] / 1e6
        ax.barh(0, wide, left=at, color=KINDS.get(r["kind"], MUTED), height=0.7, lw=0)
        for u in r["units"]:
            lane = lanes.index((u["kind"], u["x"], u["y"])) + 1
            busy = min(u["busy_d"], r["cycles"]) / 1e6
            ax.barh(
                lane,
                wide,
                left=at,
                color=KINDS.get(r["kind"], MUTED),
                alpha=0.12,
                height=0.7,
                lw=0,
            )
            ax.barh(
                lane,
                busy,
                left=at,
                color=KINDS.get(r["kind"], MUTED),
                height=0.7,
                lw=0,
            )
    ax.set_yticks(range(len(names)), names)
    ax.invert_yaxis()
    ax.set_xlabel("node cycles from the step's start (M)")
    ax.grid(axis="y", visible=False)
    ax.set_title(
        f"{out}: one warm step. Solid is unit busy, drawn from the launch's start; "
        "pale is the launch with that unit not busy"
    )
    seen = {r["kind"] for r in step}
    ax.legend(
        handles=[Patch(color=KINDS.get(k, MUTED), label=k) for k in sorted(seen)],
        ncol=len(seen),
        loc="upper right",
        bbox_to_anchor=(1, 1.22),
    )
    save(fig, f"runs/{out}", "gantt")


def split(run) -> dict:
    """Each launch's cycles under one label: a unit busy, firmware, or neither."""
    table = {"unit": 0.0, "firmware": 0.0, "neither": 0.0}
    for r in run.step:
        fw = (
            r.get("head", 0) + r.get("bind", 0) + r.get("copy", 0) + r.get("barrier", 0)
        )
        busy = max((u["busy_d"] for u in r["units"]), default=0)
        busy = min(busy, r["steps"])
        table["firmware"] += fw
        table["unit"] += busy
        table["neither"] += r["steps"] - busy
    return table


def kinds(run, out: str) -> None:
    step = run.step
    cycles = by_kind(step, "cycles")
    order = sorted(cycles, key=lambda k: -cycles[k])
    fig, axes = plt.subplots(1, 2, figsize=(14, 3.6))
    ax = axes[0]
    ax.barh(
        order,
        [cycles[k] / 1e6 for k in order],
        color=[KINDS.get(k, MUTED) for k in order],
        edgecolor=SURFACE,
    )
    total = sum(cycles.values())
    for i, k in enumerate(order):
        ax.text(
            cycles[k] / 1e6,
            i,
            f" {cycles[k] / 1e6:.1f} M, {cycles[k] / total:.0%}",
            va="center",
            color=SECOND,
        )
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node cycles (M)")
    ax.set_title(f"{out}: a warm step by kernel kind")
    ax = axes[1]
    table = split(run)
    colors = {"unit": "#2a78d6", "firmware": "#6250d6", "neither": "#eb6834"}
    stacked_bars(
        ax, ["step"], {k: [v / 1e6] for k, v in table.items()}, colors, horizontal=True
    )
    at = 0.0
    for k, v in table.items():
        ax.text(
            (at + v / 2) / 1e6,
            0.5,
            f"{k}\n{v / 1e6:.1f} M, {v / sum(table.values()):.0%}",
            ha="center",
            color=SECOND,
        )
        at += v
    ax.grid(axis="y", visible=False)
    ax.set_yticks([])
    ax.set_ylim(-0.6, 1.0)
    ax.set_xlabel("node cycles (M)")
    ax.set_title("Is any unit busy? busiest unit's cycles against the launch's")
    save(fig, f"runs/{out}", "kinds")


def pareto(run, out: str, top: int = 24) -> None:
    step = sorted(run.step, key=lambda r: -r["cycles"])[:top]
    labels = [
        f"#{r['index'] % run.length:<3} {r['name'][:10]} {r['kind']}" for r in step
    ]
    busy = [
        min(max((u["busy_d"] for u in r["units"]), default=0), r["cycles"]) / 1e6
        for r in step
    ]
    rest = [r["cycles"] / 1e6 - b for r, b in zip(step, busy, strict=True)]
    fig, ax = plt.subplots(figsize=(13, 0.32 * top + 1))
    colors = [KINDS.get(r["kind"], MUTED) for r in step]
    ax.barh(labels, busy, color=colors, edgecolor=SURFACE, label="a unit busy")
    ax.barh(
        labels,
        rest,
        left=busy,
        color=colors,
        alpha=0.3,
        edgecolor=SURFACE,
        label="no unit busy",
    )
    total = sum(r["cycles"] for r in run.step)
    for i, r in enumerate(step):
        lines = run.kernels.get(r["name"], {}).get("l1", 0)
        ax.text(
            r["cycles"] / 1e6,
            i,
            f" {r['cycles'] / 1e6:.1f} M, {r['cycles'] / total:.1%}, "
            f"{lines} L1 lines, {r['total'] / 1024:.0f} KB package",
            va="center",
            color=SECOND,
            fontsize=8,
        )
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node cycles (M)")
    ax.set_title(f"{out}: largest launches of a warm step; pale is no unit busy")
    save(fig, f"runs/{out}", "pareto")


def seam(run, out: str) -> None:
    step = run.step
    fig, axes = plt.subplots(1, 2, figsize=(14, 3.6))
    ax = axes[0]
    parts = {
        k: sum(r.get(k, 0) for r in step) / 1e6
        for k in ("head", "bind", "barrier", "idle")
    }
    names = list(parts)
    ax.bar(names, [parts[k] for k in names], color=[PHASES[k] for k in names])
    for i, k in enumerate(names):
        ax.text(
            i, parts[k], f"{parts[k]:.2f} M", ha="center", va="bottom", color=SECOND
        )
    ax.set_ylabel("node cycles a step (M)")
    ax.grid(axis="x", visible=False)
    ax.set_title(f"{out}: the seam between kernels, {len(step)} launches")
    ax = axes[1]
    steps = run.steps
    x = np.arange(len(steps))
    cycles = [sum(r["cycles"] for r in s) / 1e6 for s in steps]
    idle = [sum(r.get("idle", 0) for r in s[1:]) / 1e6 for s in steps]
    ax.bar(x, cycles, color=PHASES["steps"], label="kernels running", edgecolor=SURFACE)
    ax.bar(
        x,
        idle,
        bottom=cycles,
        color=PHASES["idle"],
        label="node idle, waiting on the host",
        edgecolor=SURFACE,
    )
    ax.set_xlabel("warm step")
    ax.set_ylabel("node cycles (M)")
    ax.grid(axis="x", visible=False)
    ax.legend(loc="upper right")
    ax.set_title("Every warm step of the run")
    save(fig, f"runs/{out}", "seam")


def main():
    name = sys.argv[1]
    run = load(name)
    out = name.split(".multimesh")[0]
    if not run.step[0].get("units") and "start" not in run.step[0]:
        return
    gantt(run, out)
    kinds(run, out)
    pareto(run, out)
    seam(run, out)


if __name__ == "__main__":
    main()
