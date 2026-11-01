"""Compiler speed over the corpus: where a compile's time goes and how it scales."""

import json

import matplotlib.pyplot as plt
from style import DATA, SECOND, SERIES, SURFACE, save

MACHINES = {"multimesh_v9t6": "2+2", "multimesh_v9t8": "4+2"}


def main():
    tables = {
        m: json.loads((DATA / "compile" / f"speed.{m}.table.json").read_text("utf-8"))
        for m in MACHINES
    }
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.2))
    for at, (machine, label) in enumerate(MACHINES.items()):
        ax = axes[at]
        t = tables[machine]
        order = sorted(t, key=lambda k: -t[k]["total_ms"])[:9]
        total = sum(v["total_ms"] for v in t.values())
        values = [t[k]["total_ms"] / 1e3 for k in order]
        ax.barh(order, values, color=SERIES[at], edgecolor=SURFACE)
        for i, k in enumerate(order):
            ax.text(
                values[i],
                i,
                f" {values[i]:.1f} s, {t[k]['total_ms'] / total:.0%}, "
                f"exponent {t[k]['exponent']:.2f}, worst {t[k]['max'] / 1e3:.1f} s",
                va="center",
                color=SECOND,
                fontsize=8,
            )
        ax.invert_yaxis()
        ax.set_xscale("log")
        ax.set_xlim(right=max(values) * 60)
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("seconds over the corpus (log)")
        ax.set_title(f"{label}: compile stages, {total / 1e3:.0f} s over 183 kernels")
    save(fig, "compiler", "stages")


if __name__ == "__main__":
    main()
