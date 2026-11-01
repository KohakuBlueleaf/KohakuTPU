"""Inside the units, on the traced model: where each vector core's dispatch stalled and
what its DMA did, and how long a cluster sat in each sequencer state. A run prints these
once, so a bar is one whole run of small kernels, never a card run."""

import collections
import re

import matplotlib.pyplot as plt
from style import DATA, SECOND, SERIES, save, stacked_bars

LOGS = {
    "anchors": "anchors.model.log",
    "lenet kernels": "lenet.model.log",
    "vit kernels": "vit.model.log",
}
DISPATCH = {
    "mq_full": "math queue full",
    "uq_full": "unpack queue full",
    "pq_full": "pack queue full",
    "aq_full": "address queue full",
    "sb_math": "scoreboard, math",
    "sb_unpack": "scoreboard, unpack",
    "fe_wait": "front end sync wait",
    "empty": "nothing to issue",
}
DMA = {
    "fill_act": "fill active",
    "drain_act": "drain active",
    "fill_sync": "fill sync wait",
    "drain_sync": "drain sync wait",
    "drain_blocked": "drain blocked",
    "drain_lost_arb": "drain lost arbitration",
}
SEQ = re.compile(r"SEQ (\d+) cu (\d+),(\d+) st (\d+)")


def fields(line: str) -> dict:
    words = line.split()[2:]
    return {k: int(v) for k, v in zip(words[::2], words[1::2], strict=False)}


def parse(text: str) -> tuple[dict, dict, dict]:
    dispatch: dict = collections.Counter()
    dma: dict = collections.Counter()
    states: dict = collections.Counter()
    last: dict = {}
    for line in text.splitlines():
        if line.startswith("V2DISPATCH"):
            dispatch.update(fields(line))
        elif line.startswith("V2DMA"):
            dma.update(fields(line))
        elif m := SEQ.match(line):
            at, unit, state = int(m[1]), (m[2], m[3]), int(m[4])
            if unit in last:
                states[last[unit][1]] += at - last[unit][0]
            last[unit] = (at, state)
    return dispatch, dma, states


def main():
    runs = {}
    for name, file in LOGS.items():
        path = DATA / "u2" / file
        if path.exists():
            runs[name] = parse(path.read_text("utf-8"))
    names = list(runs)
    fig, axes = plt.subplots(3, 1, figsize=(12, 7.5))
    tables = (DISPATCH, DMA)
    for ax, index, table, title in zip(
        axes,
        (0, 1),
        tables,
        ("Vector dispatch: cycles by stall", "Vector DMA: cycles by state"),
        strict=False,
    ):
        parts = {
            label: [runs[n][index].get(key, 0) / 1e3 for n in names]
            for key, label in table.items()
        }
        colors = dict(zip(parts, SERIES, strict=False))
        stacked_bars(ax, names, parts, colors)
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("thousand cycles, both cores")
        ax.set_title(title)
        ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.0, 1.05))
    ax = axes[2]
    every = sorted({s for n in names for s in runs[n][2] if s != 0})
    parts = {f"state {s}": [runs[n][2].get(s, 0) / 1e3 for n in names] for s in every}
    stacked_bars(ax, names, parts, dict(zip(parts, SERIES, strict=False)))
    for i, n in enumerate(names):
        idle = runs[n][2].get(0, 0) / 1e3
        total = sum(v for s, v in runs[n][2].items() if s != 0) / 1e3
        ax.text(total, i, f" idle {idle:,.0f} k", va="center", color=SECOND, fontsize=8)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("thousand cycles, every cluster, idle state left out")
    ax.set_title("Cluster sequencer: cycles by state")
    ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.0, 1.05))
    fig.tight_layout()
    save(fig, "units", "traced")


if __name__ == "__main__":
    main()
