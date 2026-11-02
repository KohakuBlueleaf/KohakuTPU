"""The whole path of a warm step, tinygrad to card units, on one time axis."""

import collections
import sys

import matplotlib.pyplot as plt
from fig_run import split
from runs import CLOCK, clock, load
from style import MUTED, SECOND, SERIES, SURFACE, save

LAYERS = (
    ("tinygrad rewrite and schedule", SERIES[0]),
    ("ktpugrad and DSL", SERIES[1]),
    ("runtime, Python", SERIES[2]),
    ("bind, Rust", SERIES[7]),
    ("driver and XDMA", SERIES[3]),
    ("Python itself", MUTED),
)


def layer(row: dict) -> str:
    f, fn = row["file"], row["fn"]
    if "_rs." in fn:
        return "bind, Rust"
    if "pwrite" in fn or "pread" in fn or "kohakuaccel/driver" in f:
        return "driver and XDMA"
    if "tinygrad/" in f:
        return "tinygrad rewrite and schedule"
    if "ktpugrad" in f or "language" in f or "/stack/" in f:
        return "ktpugrad and DSL"
    if "kohakutpu/" in f:
        return "runtime, Python"
    return "Python itself"


def host_layers(run) -> dict:
    """Own seconds by layer over the profiled launches, scaled to one step."""
    first, last = run.meta.get("profiled") or (0, 250 if "lenet" in run.name else 500)
    span = last - first
    table: dict = collections.Counter()
    for row in run.profile:
        table[layer(row)] += row["own"]
    return {k: v * run.length / span for k, v in table.items()}


def path(name: str) -> None:
    run = load(name)
    out = name.split(".multimesh")[0]
    hz = CLOCK[clock(run.meta)] * 1e6
    host = host_layers(run)
    node = split(run)
    step = run.step
    idle = sum(r.get("idle", 0) for r in step[1:])
    rows = [(k, host.get(k, 0) * 1e3, c) for k, c in LAYERS]
    rows += [
        ("node waits for the host (idle)", idle / hz * 1e3, MUTED),
        ("firmware head, bind, barrier", node["firmware"] / hz * 1e3, SERIES[6]),
        (
            "no unit busy: mover, dispatch, sync",
            node["neither"] / hz * 1e3,
            SERIES[1],
        ),
        ("a compute unit busy", node["unit"] / hz * 1e3, SERIES[0]),
    ]
    fig, ax = plt.subplots(figsize=(14, 5))
    labels = [r[0] for r in rows]
    values = [r[1] for r in rows]
    ax.barh(labels, values, color=[r[2] for r in rows], edgecolor=SURFACE)
    for i, v in enumerate(values):
        ax.text(v, i, f" {v:,.1f} ms", va="center", color=SECOND)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel(f"milliseconds a warm step, node at {hz / 1e6:.0f} MHz")
    wall = (step[-1]["start"] + step[-1]["cycles"] - step[0]["start"]) / hz * 1e3
    ax.set_title(
        f"{out}: the whole path of a warm step, top to bottom. Card span {wall:,.0f} ms. "
        "Host rows are under cProfile"
    )
    save(fig, f"runs/{out}", "path")


if __name__ == "__main__":
    path(sys.argv[1])
