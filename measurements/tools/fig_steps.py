"""Firmware-path step records: what a launch waits on, by opcode and kernel kind."""

import collections
import json
import sys

import matplotlib.pyplot as plt
from style import DATA, KINDS, MUTED, SECOND, SERIES, SURFACE, save, stacked_bars

OPS = {
    "MOVER": SERIES[5],
    "MWAIT": SERIES[2],
    "DISPATCH": SERIES[0],
    "AWAIT": SERIES[1],
    "RING": SERIES[4],
    "other": MUTED,
}


def main():
    name = sys.argv[1]
    rows = json.loads((DATA / "steps" / f"{name}.json").read_text("utf-8"))
    kinds: dict = collections.defaultdict(collections.Counter)
    for r in rows:
        for op, v in r["ops"].items():
            kinds[r["kind"]][op if op in OPS else "other"] += v
        kinds[r["kind"]]["other"] += max(r["tail"], 0)
    order = sorted(kinds, key=lambda k: -sum(kinds[k].values()))
    fig, axes = plt.subplots(1, 2, figsize=(15, 4.2), width_ratios=(1, 1.3))
    ax = axes[0]
    parts = {
        o: [kinds[k][o] / 1e6 for k in order]
        for o in OPS
        if any(kinds[k][o] for k in order)
    }
    stacked_bars(ax, order, parts, OPS)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.legend(ncol=3)
    ax.set_xlabel("node cycles over the traced run (M)")
    ax.set_title(
        f"{name}: step opcodes on the firmware path, cycles until each step ended"
    )
    top = sorted(rows, key=lambda r: -r["cycles"])[:14]
    labels = [f"{i + 1}. {r['name'][:10]} {r['kind']}" for i, r in enumerate(top)]
    parts = {o: [(r["ops"].get(o, 0)) / 1e6 for r in top] for o in OPS if o != "other"}
    ax = axes[1]
    stacked_bars(ax, labels, parts, OPS)
    for i, r in enumerate(top):
        ax.text(
            r["cycles"] / 1e6,
            i,
            f" {sum(r['counts'].values())} steps",
            va="center",
            color=SECOND,
            fontsize=8,
        )
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node cycles (M)")
    ax.set_title("Largest launches")
    fig.tight_layout()
    save(fig, "firmware", f"steps_{name}")
    print(KINDS and order, SURFACE and len(rows))


if __name__ == "__main__":
    main()
