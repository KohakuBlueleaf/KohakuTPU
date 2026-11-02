"""The path as a diagram: each stage a box, its measured cost for one warm step inside,
the box's shade by its share, so the hot stage reads before any number does."""

import collections
import sys

import matplotlib.pyplot as plt
from fig_path import host_layers
from fig_run import split
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
from runs import CLOCK, clock, load
from style import BLUES, INK, SECOND, SURFACE, save


def stages(run) -> list[tuple]:
    hz = CLOCK[clock(run.meta)] * 1e6
    host = host_layers(run)
    node = split(run)
    step = run.step
    ms = lambda cycles: cycles / hz * 1e3
    units = {(u["kind"], u["x"], u["y"]) for r in step for u in r["units"]}
    count = collections.Counter(u[0] for u in units)
    idle = sum(r.get("idle", 0) for r in step[1:])
    sent = sum(r.get("total", 0) for r in step)
    mg = sum(u["busy_d"] for r in step for u in r["units"] if u["kind"] == "MG")
    vc = sum(u["busy_d"] for r in step for u in r["units"] if u["kind"] == "VC")
    return [
        (
            "host",
            "tinygrad",
            "graph, rewrite, schedule",
            host.get("tinygrad rewrite and schedule", 0) * 1e3,
        ),
        (
            "host",
            "ktpugrad and DSL",
            "UOps to kernels",
            host.get("ktpugrad and DSL", 0) * 1e3,
        ),
        (
            "host",
            "runtime",
            "memory, plan, submit",
            host.get("runtime, Python", 0) * 1e3,
        ),
        (
            "host",
            "bind",
            f"{sent / 1e6:.1f} MB of packages",
            host.get("bind, Rust", 0) * 1e3,
        ),
        ("host", "XDMA", "writes, polls", host.get("driver and XDMA", 0) * 1e3),
        ("seam", "node waits", f"{len(step)} launches", ms(idle)),
        ("node", "firmware", "head, bind, barrier", ms(node["firmware"])),
        ("node", "mover, dispatch, sync", "no compute unit busy", ms(node["neither"])),
        ("unit", "vector cores", f"sum of busy, {count['VC']} units", ms(vc)),
        ("unit", "clusters", f"sum of busy, {count['MG']} units", ms(mg)),
    ]


def diagram(name: str) -> None:
    run = load(name)
    out = name.split(".multimesh")[0]
    rows = stages(run)
    top = max(r[3] for r in rows)
    fig, ax = plt.subplots(figsize=(15, 6))
    ax.set_xlim(0, 5)
    ax.set_ylim(0, 2.3)
    ax.axis("off")
    width, height = 0.9, 0.82
    place = [(i % 5, 1 if i < 5 else 0) for i in range(len(rows))]
    for (x, y), (where, title, what, value) in zip(place, rows, strict=True):
        level = min(int(value / top * (len(BLUES) - 1) + 0.5), len(BLUES) - 1)
        fill = BLUES[level]
        text = SURFACE if level >= 3 else INK
        ax.add_patch(
            FancyBboxPatch(
                (x + 0.05, y + 0.25),
                width,
                height,
                boxstyle="round,pad=0.0,rounding_size=0.04",
                facecolor=fill,
                edgecolor=SECOND,
                linewidth=0.8,
            )
        )
        cx = x + 0.05 + width / 2
        ax.text(cx, y + 0.9, title, ha="center", weight="bold", color=text)
        ax.text(cx, y + 0.72, what, ha="center", color=text, fontsize=9)
        ax.text(
            cx,
            y + 0.42,
            f"{value:,.0f} ms",
            ha="center",
            color=text,
            fontsize=16,
            weight="bold",
        )
    for i in range(len(rows) - 1):
        (x0, y0), (x1, y1) = place[i], place[i + 1]
        if y0 == y1:
            ax.add_patch(
                FancyArrowPatch(
                    (x0 + 0.95, y0 + 0.66),
                    (x1 + 0.05, y1 + 0.66),
                    arrowstyle="-|>",
                    mutation_scale=12,
                    color=SECOND,
                )
            )
    ax.plot([4.5, 4.5, 0.5], [1.25, 1.16, 1.16], color=SECOND, lw=1)
    ax.add_patch(
        FancyArrowPatch(
            (0.5, 1.16), (0.5, 1.07), arrowstyle="-|>", mutation_scale=12, color=SECOND
        )
    )
    ax.set_title(
        f"{out}: one warm step from tinygrad to the units; host stages under cProfile",
        loc="left",
    )
    save(fig, f"runs/{out}", "diagram")


if __name__ == "__main__":
    diagram(sys.argv[1])
