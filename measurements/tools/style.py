"""One look for every figure: the palette, marks and helpers the plot scripts share."""

import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIGURES = ROOT / "figures"
DATA = ROOT / "data"

SURFACE, INK, SECOND, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"
SERIES = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#6250d6",
    "#e34948",
)
BLUES = ("#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#184f95", "#0d366b")
#: Color follows the entity in every figure.
KINDS = {
    "convert": SERIES[0],
    "dsl": SERIES[1],
    "quant": SERIES[2],
    "matmul": SERIES[3],
    "transpose": SERIES[4],
    "move": SERIES[5],
    "copy": SERIES[6],
    "other": MUTED,
}
PHASES = {
    "head": SERIES[6],
    "bind": SERIES[4],
    "steps": SERIES[0],
    "barrier": SERIES[1],
    "idle": MUTED,
}

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "font.family": ["Segoe UI", "DejaVu Sans"],
        "font.size": 10,
        "text.color": INK,
        "axes.labelcolor": SECOND,
        "axes.edgecolor": AXIS,
        "axes.titlesize": 12,
        "axes.titleweight": "bold",
        "axes.titlelocation": "left",
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "axes.axisbelow": True,
        "grid.color": GRID,
        "grid.linewidth": 0.7,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelcolor": SECOND,
        "ytick.labelcolor": SECOND,
        "legend.frameon": False,
        "legend.fontsize": 9,
    }
)


def save(fig, perspective: str, name: str) -> pathlib.Path:
    """A figure under its perspective, at a resolution that reads on a wide screen."""
    out = FIGURES / perspective
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.png"
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return path


def stacked_bars(ax, labels, parts: dict, colors: dict, horizontal=True) -> None:
    """Bars of `parts` {series: values} with a surface gap between segments."""
    start = [0.0] * len(labels)
    for name, values in parts.items():
        draw = ax.barh if horizontal else ax.bar
        key = {"left": start} if horizontal else {"bottom": start}
        draw(
            labels,
            values,
            color=colors.get(name, MUTED),
            label=name,
            edgecolor=SURFACE,
            linewidth=1,
            **key,
        )
        start = [a + b for a, b in zip(start, values, strict=True)]
