"""This round against the zoo at commit 56021fd4, like for like: one row a model, board,
nodes, clock and TinyJit or eager. The baseline's numbers are that commit's own table;
a configuration the baseline has no run for keeps its row with no ratio."""

import csv
import io
import json
import subprocess

import matplotlib.pyplot as plt
from runs import CLOCK, by_kind, jit, load
from style import DATA, KINDS, MUTED, REPO, SECOND, SERIES, TABLES, save, stacked_bars

BASE = "56021fd4"
SHOWN = ("launches", "steps", "cycles", "span", "unit", "firmware", "neither", "idle")
#: The zoo's plain modes: a knob's or a batch's run has no counterpart here.
PLAIN = ("z", "zj", "zl", "zjl", "zs", "zjs")


def base_rows() -> dict:
    """The baseline's table, keyed by configuration. ViT's two modes were one TinyJit
    path, so they share a key."""
    path = f"{BASE}:measurements/data/zoo.csv"
    text = subprocess.run(
        ["git", "-C", str(REPO), "show", path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        if r["mode"].split()[0] in PLAIN:
            meta = json.loads((DATA / r["run"] / "meta.json").read_text("utf-8"))
            r["jit"] = jit(meta)
            out[key(r)] = r
    return out


def key(row: dict) -> tuple:
    nodes = row["run"].rsplit(".", 1)[1]
    return row["model"], row["board"], nodes, row["clock"], str(row["jit"])


def label(row: dict) -> str:
    board = "4+2" if row["board"] == "v9t8" else "2+2"
    mode = "TinyJit" if str(row["jit"]) == "True" else "eager"
    nodes = row["run"].rsplit(".", 1)[1]
    return f"{row['model']} {board} {nodes} {row['clock']} {mode}"


def joined() -> list[tuple[dict, dict | None]]:
    base = base_rows()
    with (TABLES / "zoo.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [(r, base.get(key(r))) for r in rows]


def step_ms(row: dict) -> float:
    """A warm step's cadence in ms at the row's node clock, capture and replay alike."""
    return load(row["run"]).cadence / CLOCK[row["clock"]] / 1e3


def table(pairs: list[tuple[dict, dict | None]]) -> None:
    head = ["configuration", "run", "base_run", "ratio", "step_ms", "base_step_ms"]
    for k in SHOWN:
        head += [k, f"base_{k}"]
    with (TABLES / "compare.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(head)
        for r, b in pairs:
            ratio = int(r["cycles"]) / int(b["cycles"]) if b else ""
            row = [label(r), r["run"], b["run"] if b else "", ratio]
            row += [step_ms(r), step_ms(b) if b else ""]
            for k in SHOWN:
                row += [r[k], b[k] if b else ""]
            w.writerow(row)


def cycles_chart(pairs: list[tuple[dict, dict]]) -> None:
    """Each configuration's two warm steps by kind, the baseline's bar first."""
    pairs = sorted(pairs, key=lambda p: -int(p[0]["cycles"]))
    labels, kinds = [], []
    for r, b in pairs:
        labels += [f"{label(r)}, base", label(r)]
        kinds += [by_kind(load(b["run"]).step, "cycles")]
        kinds += [by_kind(load(r["run"]).step, "cycles")]
    every = sorted({k for row in kinds for k in row})
    parts = {k: [row.get(k, 0) / 1e6 for row in kinds] for k in every}
    fig, ax = plt.subplots(figsize=(13, 0.34 * len(labels) + 1.4))
    stacked_bars(ax, labels, parts, KINDS)
    for i, (r, b) in enumerate(pairs):
        now, then = int(r["cycles"]), int(b["cycles"])
        text = f" {now / 1e6:.1f} M, {now / then:.2f}× of {then / 1e6:.1f} M"
        ax.text(now / 1e6, 2 * i + 1, text, va="center", color=SECOND, fontsize=8)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node cycles a warm step (M)")
    ax.legend(ncol=len(every), loc="lower right")
    ax.set_title(f"This round against {BASE}")
    save(fig, "compare", "cycles")


def launches_chart(pairs: list[tuple[dict, dict]]) -> None:
    """Launches and span of a warm step as ratios to the baseline's."""
    pairs = sorted(pairs, key=lambda p: label(p[0]))
    labels = [label(r) for r, _ in pairs]
    fig, axes = plt.subplots(1, 2, figsize=(13, 0.3 * len(labels) + 1.4), sharey=True)
    for ax, what, color in zip(axes, ("launches", "span"), SERIES, strict=False):
        ratios = [int(r[what]) / int(b[what]) for r, b in pairs]
        ax.barh(labels, ratios, color=color)
        ax.axvline(1, color=MUTED, linewidth=1)
        for i, v in enumerate(ratios):
            ax.text(v, i, f" {v:.2f}×", va="center", color=SECOND, fontsize=8)
        ax.grid(axis="y", visible=False)
        ax.set_xlabel(f"{what} a warm step over the baseline's")
    axes[0].invert_yaxis()
    axes[0].set_title(f"Against {BASE}: line at 1 is unchanged")
    save(fig, "compare", "launches")


def replay_chart(pairs: list[tuple[dict, dict]]) -> None:
    """TinyJit's replayed step against the baseline's warm step, at each clock."""
    rows = [(r, step_ms(r), step_ms(b)) for r, b in pairs if str(r["jit"]) == "True"]
    rows = sorted(rows, key=lambda x: label(x[0]))
    labels = [label(r) for r, _, _ in rows]
    fig, ax = plt.subplots(figsize=(11, 0.3 * len(labels) + 1.4))
    ratios = [now / then for _, now, then in rows]
    ax.barh(labels, ratios, color=SERIES[0])
    ax.axvline(1, color=MUTED, linewidth=1)
    for i, ((_, now, then), v) in enumerate(zip(rows, ratios, strict=True)):
        text = f" {now:.0f} ms against {then:.0f} ms"
        ax.text(v, i, text, va="center", color=SECOND, fontsize=8)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("replayed step over the baseline's warm step")
    ax.set_title(f"TinyJit step cadence against {BASE}: line at 1 is unchanged")
    save(fig, "compare", "replay")


def main():
    pairs = joined()
    table(pairs)
    matched = [(r, b) for r, b in pairs if b]
    if matched:
        cycles_chart(matched)
        launches_chart(matched)
        replay_chart(matched)
    print(len(matched), "of", len(pairs), "rows have a baseline")


if __name__ == "__main__":
    main()
