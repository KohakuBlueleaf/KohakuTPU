"""Models against each other: every packed run on one set of axes, and its raw table."""

import csv
import gzip
import json
import re

import matplotlib.pyplot as plt
from fig_run import split
from runs import by_kind, load, names
from style import (
    DATA,
    FIGURES,
    KINDS,
    MUTED,
    SECOND,
    SERIES,
    SURFACE,
    save,
    stacked_bars,
)

CLOCK = {"low": 100, "mid": 200, "ship": 300}
FORM_KINDS = ("quant", "convert", "transpose", "copy", "move")


def floors(name: str, step: list[dict], span: int) -> tuple[float, float]:
    """Math-only floors over the span: a floor is already per unit of its class, all
    units running. Form changes count as no useful work."""
    machine = re.search(r"multimesh_\w+", name)[0]
    path = DATA / name / f"timed.{machine}.jsonl.gz"
    if not path.exists():
        return 0.0, 0.0
    rows = [
        json.loads(x) for x in gzip.decompress(path.read_bytes()).decode().splitlines()
    ]
    table = {r["name"]: r["floor"] for r in rows}
    mg = vc = 0.0
    for r in step:
        if r["kind"] in FORM_KINDS:
            continue
        f = table.get(r["name"], {})
        mg += f.get("mg", 0.0)
        vc += f.get("vc", 0.0)
    return mg / span, vc / span


def row(name: str) -> dict | None:
    run = load(name)
    step = run.step
    if not step or "start" not in step[0]:
        return None
    tag = name.split(".multimesh")[0]
    model, _, mode = tag.partition(".")
    nodes = name.rsplit(".", 1)[1]
    mode += "" if nodes == "1n" else f" {nodes}"
    mode += " 4+2" if "v9t8" in name else ""
    span = step[-1]["start"] + step[-1]["cycles"] - step[0]["start"]
    node = split(run)
    totals = sorted(sum(r["cycles"] for r in s) for s in run.steps)
    units = {}
    for r in step:
        for u in r["units"]:
            key = f"{u['kind']}{u['x']}{u['y']}"
            units[key] = units.get(key, 0) + u["busy_d"]
    mfu = floors(name, step, span)
    return {
        "run": name,
        "model": model,
        "mfu_mg": mfu[0],
        "mfu_vc": mfu[1],
        "mode": mode,
        "board": re.search(r"multimesh_(\w+)", name)[1],
        "clock": run.meta.get("profile") or "mid",
        "launches": len(step),
        "steps": len(run.steps),
        "cycles": sum(r["cycles"] for r in step),
        "spread": (totals[-1] - totals[0]) / totals[len(totals) // 2],
        "span": span,
        "idle": sum(r.get("idle", 0) for r in step[1:]),
        "firmware": node["firmware"],
        "unit": node["unit"],
        "neither": node["neither"],
        "kinds": by_kind(step, "cycles"),
        "units": units,
        "submit_ms": sum(r.get("submit_ms", 0) for r in step),
        "gap_ms": sum(r.get("gap_ms", 0) for r in step),
    }


def table(rows: list[dict]) -> None:
    keys = [k for k in rows[0] if k not in ("kinds", "units")]
    path = DATA / "zoo.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(keys)
        for r in rows:
            w.writerow([r[k] for k in keys])


def kinds_chart(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: r["cycles"])
    labels = [f"{r['model']} {r['mode']}" for r in rows]
    every = sorted({k for r in rows for k in r["kinds"]})
    parts = {k: [r["kinds"].get(k, 0) / 1e6 for r in rows] for k in every}
    fig, ax = plt.subplots(figsize=(13, 0.4 * len(rows) + 1.4))
    stacked_bars(ax, labels, parts, KINDS)
    for i, r in enumerate(rows):
        ax.text(
            r["cycles"] / 1e6,
            i,
            f" {r['cycles'] / 1e6:.1f} M, {r['launches']} launches",
            va="center",
            color=SECOND,
            fontsize=8,
        )
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node cycles a warm step (M)")
    ax.legend(ncol=len(every), loc="lower right")
    ax.set_title("Each model's warm step by kernel kind")
    save(fig, "zoo", "kinds")


def share_chart(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: r["neither"] / (r["span"] or 1))
    labels = [f"{r['model']} {r['mode']}" for r in rows]
    parts = {
        "a unit busy": [r["unit"] / r["span"] for r in rows],
        "firmware": [r["firmware"] / r["span"] for r in rows],
        "no unit busy": [r["neither"] / r["span"] for r in rows],
        "node idle, host": [
            max(0, 1 - (r["unit"] + r["firmware"] + r["neither"]) / r["span"])
            for r in rows
        ],
    }
    colors = {
        "a unit busy": SERIES[0],
        "firmware": SERIES[6],
        "no unit busy": SERIES[1],
        "node idle, host": MUTED,
    }
    fig, ax = plt.subplots(figsize=(12, 0.4 * len(rows) + 1.4))
    stacked_bars(ax, labels, parts, colors)
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("share of a warm step's span on the node")
    ax.legend(ncol=4, loc="upper center", bbox_to_anchor=(0.5, 1.13))
    ax.set_title("What the node's time was doing", pad=26)
    save(fig, "zoo", "shares")


def busy_chart(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: r["model"])
    fig, ax = plt.subplots(figsize=(12, 0.45 * len(rows) + 1.4))
    labels = [f"{r['model']} {r['mode']}" for r in rows]
    for y, r in enumerate(rows):
        at = 0.0
        for key in sorted(r["units"]):
            share = r["units"][key] / r["span"]
            color = SERIES[0] if key.startswith("VC") else SERIES[3]
            ax.barh(y, share, left=at, color=color, edgecolor=SURFACE)
            at += share
        ax.text(at, y, f" {at:.2f} unit-spans", va="center", color=SECOND, fontsize=8)
    ax.set_yticks(range(len(labels)), labels)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel(
        "sum over units of busy / span; 4.0 is all four units busy all the time"
    )
    ax.set_title("Unit busy: blue vector cores, yellow clusters")
    save(fig, "zoo", "unit_busy")


def mfu_chart(rows: list[dict]) -> None:
    rows = sorted(
        (r for r in rows if r["mfu_mg"] or r["mfu_vc"]),
        key=lambda r: r["mfu_mg"] + r["mfu_vc"],
    )
    if not rows:
        return
    labels = [f"{r['model']} {r['mode']}" for r in rows]
    parts = {
        "cluster MFU": [r["mfu_mg"] for r in rows],
        "vector MFU": [r["mfu_vc"] for r in rows],
    }
    colors = {"cluster MFU": SERIES[3], "vector MFU": SERIES[0]}
    fig, ax = plt.subplots(figsize=(11, 0.4 * len(rows) + 1.4))
    stacked_bars(ax, labels, parts, colors)
    for i, r in enumerate(rows):
        ax.text(
            r["mfu_mg"] + r["mfu_vc"],
            i,
            f" {r['mfu_mg']:.2%} + {r['mfu_vc']:.2%}",
            va="center",
            color=SECOND,
            fontsize=8,
        )
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.1%}")
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower right")
    ax.set_title("MFU: math-only floor cycles over the span, all clusters and cores")
    save(fig, "zoo", "mfu")


def clock_chart(rows: list[dict]) -> None:
    groups: dict = {}
    for r in rows:
        if r["mode"] in ("z", "zl", "zs"):
            groups.setdefault(r["model"], {})[r["clock"]] = r
    chosen = {m: g for m, g in groups.items() if len(g) >= 2}
    if not chosen:
        return
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.4))
    for at, (model, g) in enumerate(sorted(chosen.items())):
        order = [c for c in CLOCK if c in g]
        mhz = [CLOCK[c] for c in order]
        base = g[order[0]]["cycles"]
        style = {
            "color": SERIES[at % 8],
            "marker": "o",
            "ms": 8,
            "lw": 2,
            "label": model,
        }
        axes[0].plot(mhz, [g[c]["cycles"] / base for c in order], **style)
        wall = [g[c]["cycles"] / CLOCK[c] / 1e3 for c in order]
        axes[1].plot(mhz, [w / wall[0] for w in wall], **style)
        for x, w, ratio in zip(mhz, wall, (w / wall[0] for w in wall), strict=True):
            axes[1].annotate(
                f"{w:.0f} ms",
                (x, ratio),
                textcoords="offset points",
                xytext=(0, 10 + 11 * at if x == mhz[0] else 8),
                ha="center",
                color=SECOND,
                fontsize=8,
            )
    axes[1].plot(
        [100, 200, 300], [1, 1 / 2, 1 / 3], color=MUTED, lw=1, ls="--", label="ideal"
    )
    axes[0].set_ylabel("step cycles against the lowest clock")
    axes[0].set_title("Cycles a step rise with the clock")
    axes[1].set_ylabel("step time against the lowest clock")
    axes[1].set_title("Step time falls far less than the clock rises")
    for ax in axes:
        ax.set_xlabel("node clock, MHz")
        ax.legend()
    save(fig, "zoo", "clock")


def wall_chart(rows: list[dict]) -> None:
    """Eager against TinyJit at one clock: a step's span in time, node work inside it."""
    mhz = CLOCK["mid"] * 1e3
    rows = [r for r in rows if r["mode"] in ("z", "zj") and r["clock"] == "mid"]
    rows.sort(key=lambda r: (r["span"], r["mode"]))
    labels = [
        f"{r['model']} {'TinyJit' if r['mode'] == 'zj' else 'eager'}" for r in rows
    ]
    parts = {
        "launches running": [r["cycles"] / mhz for r in rows],
        "node waiting for the host": [(r["span"] - r["cycles"]) / mhz for r in rows],
    }
    colors = {"launches running": SERIES[0], "node waiting for the host": MUTED}
    fig, ax = plt.subplots(figsize=(11, 0.4 * len(rows) + 1.6))
    stacked_bars(ax, labels, parts, colors)
    for i, r in enumerate(rows):
        ax.text(
            r["span"] / mhz,
            i,
            f" {r['span'] / mhz:.0f} ms",
            va="center",
            color=SECOND,
            fontsize=8,
        )
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("ms a warm step at mid")
    ax.legend(ncol=2, loc="lower right")
    ax.set_title("A warm step's wall time: eager against TinyJit")
    save(fig, "zoo", "wall")


def knob_chart(rows: list[dict]) -> None:
    """One knob or batch size at a time against the same model's default run."""
    labels = {
        "z": "default",
        "zb32": "batch 32",
        "zb128": "batch 128",
        "znp": "prepared off",
        "znc": "composed off",
    }
    chosen = [r for r in rows if r["model"] in ("mlp", "lenet") and r["mode"] in labels]
    chosen.sort(key=lambda r: (r["model"], r["cycles"]))
    names_ = [f"{r['model']}, {labels[r['mode']]}" for r in chosen]
    fig, ax = plt.subplots(figsize=(10, 0.42 * len(chosen) + 1.4))
    stacked_bars(
        ax,
        names_,
        {"cycles": [r["cycles"] / 1e6 for r in chosen]},
        {"cycles": SERIES[0]},
    )
    for i, r in enumerate(chosen):
        text = f" {r['cycles'] / 1e6:.1f} M, {r['launches']} launches"
        ax.text(r["cycles"] / 1e6, i, text, va="center", color=SECOND, fontsize=8)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node cycles a warm step (M)")
    ax.set_title("Batch size and stack knobs")
    save(fig, "zoo", "knobs")


def main():
    rows = [r for r in (row(n) for n in names()) if r]
    table(rows)
    kinds_chart(rows)
    share_chart(rows)
    busy_chart(rows)
    mfu_chart(rows)
    clock_chart(rows)
    wall_chart(rows)
    knob_chart(rows)
    print(len(rows), "runs", FIGURES)


if __name__ == "__main__":
    main()
