"""The memory grid: cycles a 32-byte word by footprint and stride, per agent and clock."""

import json
import re
import runpy

import matplotlib.pyplot as plt
import numpy as np
from style import DATA, INK, REPO, SERIES, SURFACE, save

GRID = runpy.run_path(str(REPO / "software" / "tests" / "perf" / "memgrid.py"))
CLOCKS = ("low", "mid", "ship")
AGENTS = {
    "vr": "vector read",
    "vw": "vector write",
    "vr2": "two vectors read",
    "vw2": "two vectors write",
    "mv": "mover copy",
}
NAME = re.compile(r"(\w+?)_(\d+)k_(\d+)$")


def rows(folder: str = "grid") -> list[dict]:
    out = []
    for clock in CLOCKS:
        for path in sorted((DATA / folder).glob(f"{clock}.*.jsonl")):
            for line in path.read_text("utf-8").splitlines():
                row = json.loads(line)
                m = NAME.match(row["name"])
                if "error" in row or m is None:
                    continue
                agent, kb, stride = m[1], int(m[2]), int(m[3])
                size = kb * 1024
                words = size / stride
                if agent != "mv":
                    words = GRID["loads"](size, stride) * GRID["burst"](stride)
                    words *= 2 if agent.endswith("2") else 1
                out.append(
                    {
                        "clock": clock,
                        "agent": agent,
                        "kb": kb,
                        "stride": stride,
                        "warm": row["cycles"] / words,
                        "cold": row["cold"] / words,
                    }
                )
    return out


def heatmaps(data: list[dict], key: str, name: str) -> None:
    agents = {a: t for a, t in AGENTS.items() if any(r["agent"] == a for r in data)}
    kbs = sorted({r["kb"] for r in data})
    strides = sorted({r["stride"] for r in data})
    clocks = [c for c in CLOCKS if any(r["clock"] == c for r in data)]
    fig, axes = plt.subplots(
        len(clocks),
        len(agents),
        figsize=(3.4 * len(agents), 2.9 * len(clocks)),
        squeeze=False,
        sharex=True,
        sharey=True,
    )
    top = np.percentile([r[key] for r in data], 97)
    for i, clock in enumerate(clocks):
        for j, (agent, title) in enumerate(agents.items()):
            ax = axes[i][j]
            grid = np.full((len(strides), len(kbs)), np.nan)
            for r in data:
                if r["clock"] == clock and r["agent"] == agent:
                    grid[strides.index(r["stride"]), kbs.index(r["kb"])] = r[key]
            ax.imshow(
                np.log2(np.clip(grid, 1, None)),
                cmap="Blues",
                aspect="auto",
                vmin=0,
                vmax=np.log2(top),
            )
            for (y, x), v in np.ndenumerate(grid):
                if not np.isnan(v):
                    dark = np.log2(max(v, 1)) > 0.6 * np.log2(top)
                    ax.text(
                        x,
                        y,
                        f"{v:.1f}" if v < 100 else f"{v:.0f}",
                        ha="center",
                        va="center",
                        fontsize=7,
                        color=SURFACE if dark else INK,
                    )
            ax.set_xticks(
                range(len(kbs)),
                [f"{k // 1024}M" if k >= 1024 else f"{k}K" for k in kbs],
            )
            ax.set_yticks(range(len(strides)), strides)
            ax.grid(visible=False)
            ax.set_title(f"{title}, {clock}", fontsize=10)
            if j == 0:
                ax.set_ylabel("stride, bytes")
            if i == len(clocks) - 1:
                ax.set_xlabel("footprint")
    fig.suptitle(
        f"Node cycles a 32-byte word, {key} pass; a two-vector cell counts both cores' words",
        x=0.02,
        y=1.06,
        ha="left",
        weight="bold",
    )
    save(fig, "memory", name)


def clocks(data: list[dict]) -> None:
    """Contiguous reads against clock: DRAM does not scale with the node."""
    fig, ax = plt.subplots(figsize=(10, 4))
    for at, agent in enumerate(("vr", "vw", "mv")):
        for clock, style in zip(CLOCKS, ("-", "--", ":"), strict=True):
            line = sorted(
                (r for r in data if r["agent"] == agent and r["clock"] == clock),
                key=lambda r: r["kb"],
            )
            line = [r for r in line if r["stride"] == 32]
            if line:
                ax.plot(
                    [r["kb"] for r in line],
                    [r["cold"] for r in line],
                    style,
                    color=SERIES[at],
                    marker="o",
                    ms=4,
                    label=f"{AGENTS[agent]}, {clock}",
                )
    ax.set_xscale("log", base=2)
    ax.set_xlabel("footprint, KB")
    ax.set_ylabel("node cycles a word, cold")
    ax.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.16))
    ax.set_title("Dense walks by footprint and clock")
    save(fig, "memory", "clock_scaling")


def homes() -> None:
    """One line 64 KB apart per load, four start offsets 16 KB apart: a load's cycles."""
    fig, ax = plt.subplots(figsize=(8, 3.4))
    at = 0
    for i, clock in enumerate(CLOCKS):
        path = DATA / "grid" / f"{clock}.home.jsonl"
        if not path.exists():
            continue
        got = [json.loads(x) for x in path.read_text("utf-8").splitlines()]
        got = [r for r in got if "error" not in r]
        x = [at + k for k in range(len(got))]
        ax.bar(x, [r["cycles"] / 512 for r in got], color=SERIES[i], label=clock)
        at += len(got) + 1
    ax.set_xticks([])
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("node cycles a two-word load")
    ax.set_xlabel("start offset 0, 16, 32, 48 KB, at each clock")
    ax.legend()
    ax.set_title("A 64 KB walk by start offset")
    save(fig, "memory", "homes")


def main():
    data = rows()
    homes()
    heatmaps(data, "warm", "grid_warm")
    heatmaps(data, "cold", "grid_cold")
    heatmaps(rows("grid42"), "warm", "grid_42")
    clocks(data)


if __name__ == "__main__":
    main()
