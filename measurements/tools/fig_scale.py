"""2+2 against 4+2 on the card at mid: a warm step's cycles by kernel kind, with what the
timed model predicted for each."""

import json

import matplotlib.pyplot as plt
import numpy as np
from runs import by_kind, load
from style import DATA, KINDS, MUTED, SECOND, SERIES, save, stacked_bars

MODELS = ("mlp", "lenet", "vit", "mixer", "gru", "gpt", "wide")
BOARDS = {"2+2": "multimesh_v9t6", "4+2": "multimesh_v9t8"}
#: (run tag, label, node MHz)
TAGS = (("zl", "low, 100", 100), ("z", "mid, 200", 200), ("zs", "ship, 300", 300))
WALLS = (
    ("zl", "low"),
    ("zjl", "low jit"),
    ("z", "mid"),
    ("zj", "mid jit"),
    ("zs", "ship"),
    ("zjs", "ship jit"),
)


def main():
    rows = []
    for model in MODELS:
        for board, machine in BOARDS.items():
            name = f"{model}.z.{machine}.1n"
            step = load(name).step
            waste = json.loads((DATA / name / "waste.json").read_text("utf-8"))
            rows.append((f"{model} {board}", step, waste["step_cycles"]))
    kinds_by_row = [by_kind(step, "cycles") for _, step, _ in rows]
    kinds = sorted({k for row in kinds_by_row for k in row})
    parts = {k: [row.get(k, 0) / 1e6 for row in kinds_by_row] for k in kinds}
    fig, ax = plt.subplots(figsize=(12, 0.5 * len(rows) + 1.8))
    colors = {k: KINDS.get(k, MUTED) for k in kinds}
    stacked_bars(ax, [r[0] for r in rows], parts, colors)
    for i, (_, step, modelled) in enumerate(rows):
        total = sum(r["cycles"] for r in step)
        text = (
            f" {total / 1e6:.1f} M, {len(step)} launches, model {modelled / 1e6:.1f} M"
        )
        ax.text(total / 1e6, i, text, va="center", color=SECOND, fontsize=8)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node cycles a warm step on the card (M)")
    ax.legend(ncol=len(kinds), loc="lower right")
    ax.set_title("A warm step on 2+2 and on 4+2")
    save(fig, "zoo", "boards")
    clocks("multimesh_v9t6", "2+2", "clocks22")
    clocks("multimesh_v9t8", "4+2", "clocks42")
    unit_share()
    idle_dies()
    wall()


def clocks(machine: str, board: str, out: str) -> None:
    """Per model, a warm step's cycles and device time at each clock."""
    fig, axes = plt.subplots(1, 2, figsize=(12, 0.5 * len(MODELS) + 1.8), sharey=True)
    at = range(len(MODELS))
    height = 0.8 / len(TAGS)
    for i, (tag, label, mhz) in enumerate(TAGS):
        cycles = []
        for model in MODELS:
            name = f"{model}.{tag}.{machine}.1n"
            ok = (DATA / name / "meta.json").exists()
            run = load(name) if ok else None
            cycles.append(sum(r["cycles"] for r in run.step) if run and run.step else 0)
        y = [a + i * height for a in at]
        axes[0].barh(
            y, [c / 1e6 for c in cycles], height=height, color=SERIES[i], label=label
        )
        axes[1].barh(y, [c / mhz / 1e3 for c in cycles], height=height, color=SERIES[i])
    axes[0].set_yticks([a + height for a in at], MODELS)
    axes[0].set_xlabel("node cycles a warm step (M)")
    axes[1].set_xlabel("device time a warm step (ms), cycles over the clock")
    for ax in axes:
        ax.grid(axis="y", visible=False)
    axes[0].legend(loc="lower right")
    axes[0].set_title(f"{board} by clock")
    save(fig, "zoo", out)


def idle_dies() -> None:
    """One Mixer step on node 0 at mid, by the clock of the dies that run nothing."""
    rows = (
        ("zn5", "others low"),
        ("zn6", "node 1 ship, others low"),
        ("zn4", "others mid"),
        ("zn3", "others ship"),
    )
    fig, ax = plt.subplots(figsize=(11, 2.6))
    for tag, label in rows:
        step = load(f"mixer.{tag}.multimesh_v9t6.1n").step
        cycles = sum(r["cycles"] for r in step) / 1e6
        ax.barh(label, cycles, color=SERIES[0])
        ax.text(cycles, label, f" {cycles:.2f} M", va="center", color=SECOND)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node 0 cycles a warm step, node 0 at mid")
    ax.set_title("Dies that run nothing still change node 0's cycles")
    save(fig, "zoo", "idle_dies")


def unit_share() -> None:
    """Share of a warm step each unit is busy, at mid, one panel a board."""
    fig, axes = plt.subplots(1, 2, figsize=(12, 3.8), width_ratios=(4, 6), sharey=True)
    for ax, machine in zip(axes, BOARDS.values(), strict=True):
        busy = {}
        for model in MODELS:
            step = load(f"{model}.z.{machine}.1n").step
            total = sum(r["cycles"] for r in step)
            for r in step:
                for u in r.get("units", ()):
                    key = (u["kind"], u["x"], u["y"])
                    busy[model, key] = busy.get((model, key), 0) + u["busy_d"] / total
        units = sorted({key for _, key in busy}, key=lambda k: (k[0] != "VC", k[2]))
        grid = np.array([[busy.get((m, u), 0) for u in units] for m in MODELS])
        ax.imshow(grid, cmap="Blues", aspect="auto", vmin=0, vmax=1)
        for (y, x), v in np.ndenumerate(grid):
            tone = "white" if v > 0.5 else "black"
            ax.text(x, y, f"{v:.0%}", ha="center", va="center", color=tone, fontsize=8)
        ax.set_xticks(range(len(units)), [f"{u[0]} {u[1]},{u[2]}" for u in units])
        ax.set_yticks(range(len(MODELS)), MODELS)
        ax.grid(visible=False)
        ax.set_title(f"{machine}: busy share of a warm step, mid")
    save(fig, "zoo", "unit_share")


def wall() -> None:
    """Whole run's wall seconds, each model by clock and mode, one panel a board."""
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2), sharey=True)
    for ax, (board, machine) in zip(axes, BOARDS.items(), strict=True):
        grid = np.full((len(MODELS), len(WALLS)), np.nan)
        for y, model in enumerate(MODELS):
            for x, (tag, _) in enumerate(WALLS):
                meta = DATA / f"{model}.{tag}.{machine}.1n" / "meta.json"
                if meta.exists():
                    doc = json.loads(meta.read_text("utf-8"))
                    if doc.get("done") == "exited":
                        grid[y, x] = doc["seconds"]
        ax.imshow(np.log(grid), cmap="Blues", aspect="auto", vmin=0, vmax=5.5)
        for (y, x), v in np.ndenumerate(grid):
            text = "-" if np.isnan(v) else f"{v:.1f}"
            tone = "white" if v > 15 else "black"
            ax.text(x, y, text, ha="center", va="center", color=tone, fontsize=8)
        ax.set_xticks(range(len(WALLS)), [w[1] for w in WALLS])
        ax.set_yticks(range(len(MODELS)), MODELS)
        ax.grid(visible=False)
        ax.set_title(f"{board}: wall seconds of the run")
    save(fig, "zoo", "wall_matrix")


if __name__ == "__main__":
    main()
