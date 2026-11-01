"""The timed model against the card, kernel by kernel, and useful work as MFU."""

import collections
import gzip
import json
import sys

import matplotlib.pyplot as plt
from runs import load
from style import DATA, KINDS, MUTED, SECOND, SURFACE, save

MACHINE = "multimesh_v9t6"


def timed(name: str) -> dict:
    raw = (DATA / name / f"timed.{MACHINE}.jsonl.gz").read_bytes()
    rows = [json.loads(x) for x in gzip.decompress(raw).decode().splitlines()]
    return {r["name"]: r for r in rows}


def main():
    name = sys.argv[1]
    run = load(name)
    out = name.split(".multimesh")[0]
    model = timed(name)
    card: dict = {}
    for r in run.step:
        e = card.setdefault(r["name"], {"kind": r["kind"], "cycles": 0, "n": 0})
        e["cycles"] += r["cycles"]
        e["n"] += 1
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.4))
    ax = axes[0]
    kinds = collections.defaultdict(lambda: [0.0, 0.0])
    for n, e in card.items():
        m = model.get(n, {}).get("cycles", 0) * e["n"]
        kinds[e["kind"]][0] += e["cycles"]
        kinds[e["kind"]][1] += m
        ax.scatter(
            e["cycles"] / e["n"],
            max(m / e["n"], 1),
            s=26,
            color=KINDS.get(e["kind"], MUTED),
            edgecolor=SURFACE,
            linewidth=0.5,
            label=e["kind"],
        )
    top = max(e["cycles"] / e["n"] for e in card.values())
    ax.plot([1e3, top], [1e3, top], color=MUTED, lw=1)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("card cycles a launch")
    ax.set_ylabel("model cycles a launch")
    handles, labels = ax.get_legend_handles_labels()
    keep = dict(zip(labels, handles, strict=True))
    ax.legend(keep.values(), keep.keys())
    ax.set_title(f"{out}: every kernel of a warm step, model against card")
    ax = axes[1]
    order = sorted(kinds, key=lambda k: -kinds[k][0])
    x = range(len(order))
    ax.bar(
        [i - 0.2 for i in x],
        [kinds[k][0] / 1e6 for k in order],
        width=0.4,
        color=[KINDS.get(k, MUTED) for k in order],
        label="card",
    )
    ax.bar(
        [i + 0.2 for i in x],
        [kinds[k][1] / 1e6 for k in order],
        width=0.4,
        color=[KINDS.get(k, MUTED) for k in order],
        alpha=0.4,
        label="model",
    )
    for i, k in enumerate(order):
        err = kinds[k][1] / kinds[k][0] - 1
        ax.text(
            i,
            max(kinds[k]) / 1e6,
            f"{err:+.0%}",
            ha="center",
            va="bottom",
            color=SECOND,
        )
    ax.set_xticks(list(x), order)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("node cycles a step (M); solid card, pale model")
    total = sum(v[0] for v in kinds.values())
    wrong = sum(abs(v[1] - v[0]) for v in kinds.values())
    ax.set_title(f"By kind: {wrong / total:.0%} of cycles misplaced")
    save(fig, "model", out)


if __name__ == "__main__":
    main()
