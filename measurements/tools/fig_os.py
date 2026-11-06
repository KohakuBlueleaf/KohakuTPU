"""What a launch costs on each dispatch path, what that path would cost a step, and where
the OS kernel's own cycles go. Every number is this zoo's on silicon, one die at low: the
firmware row from the MLP run, the OS rows from `data/os`."""

import csv
import json
import statistics

import matplotlib.pyplot as plt
from runs import load, run_name
from style import DATA, SECOND, SERIES, TABLES, save

STAGES = ("enter", "save", "to_c", "call", "poll", "schedule", "to_resume", "out")
CALLS = ("self", "yield", "send_here", "recv_ready")
PRIMITIVES = (
    ("bench.l0", "L0 call"),
    ("bench.syscall", "null system call"),
    ("bench.barrier", "barrier, one node"),
    ("bench.yield", "yield, two switches"),
    ("bench.spawn_local", "spawn and join"),
    ("bench.mmap", "4-page mmap"),
    ("kdsbench.alloc_here", "dispatcher alloc and free"),
)


FAR = (
    ("bench.atomic_local", "atomic, this node"),
    ("bench.atomic_remote", "atomic, another die"),
    ("bench.release", "cache release sweep"),
    ("bench.acquire", "cache acquire sweep"),
    ("bench.barrier", "barrier, four nodes"),
    ("bench.chan_rtt", "channel round trip"),
    ("kdsbench.chain", "dispatcher chain task"),
    ("kdsbench.hop", "dispatcher chain hop, another die"),
    ("kdsbench.load", "dispatcher task, loaded on the last node"),
    ("kdschain.cycles_per_step", "dispatcher chain step, four nodes"),
)
CAPTURES = (("vit", "ViT, 40 launches"), ("qkv", "ViT-QKV, 8 launches"))
CAPTURES_42 = (("vit", "ViT, 48 launches"), ("qkv", "ViT-QKV, 9 launches"))
CLOCKS = (("low", "t6l", "t8l"), ("mid", "rp", "t8m"), ("ship", "t6s", "t8s"))
#: (run, label), stamps off in every OS row.
REPLAY = (
    ("fw", "firmware"),
    ("fwl", "firmware, host list"),
    ("osn", "OS graph"),
    ("osnr", "OS graph, retained"),
    ("osf", "OS fused"),
    ("osfr", "OS fused, retained"),
)
CLOCKED = (
    ("bench.syscall", "null system call"),
    ("bench.yield", "yield"),
    ("bench.barrier", "barrier"),
    ("bench.spawn_local", "spawn and join"),
    ("bench.mmap", "4-page mmap"),
    ("kdsbench.one", "one-task graph"),
    ("kdsbench.chain", "chain task"),
    ("kdschain.cycles_per_step", "chain step"),
    ("mover.local_cycles", "mover copy"),
)


def measures(run: str) -> dict[str, int]:
    doc = json.loads((DATA / "os" / run / "run.json").read_text(encoding="utf-8"))
    return doc["measures"]["0"]


def firmware_launch() -> int:
    """The median launch's head, bind and barrier, MLP at low: this round's, the zoo's
    in a round that did not run low."""
    name = run_name("mlp", "zl", "multimesh_v9t6")
    name = name if (DATA / name).exists() else "mlp.zl.multimesh_v9t6.1n"
    step = load(name).step
    return int(statistics.median(r["head"] + r["bind"] + r["barrier"] for r in step))


def path_rows() -> list[tuple[str, int, str]]:
    """(label, cycles a launch, source)."""
    mail, engine = measures("t6low.all"), measures("t6low.engine")
    round_mail = measures("t6low.kdsmg")["kdsmg.round_cycles"]
    round_engine = measures("t6low.kdsmge")["kdsmg.round_cycles"]
    return [
        (
            "firmware, host-lowered package (head, bind, barrier)",
            firmware_launch(),
            "firmware",
        ),
        ("kernel, MG mailbox round", mail["mg.mailbox_round_cycles"], "kernel"),
        ("kernel, MG engine round", engine["mg.engine_round_cycles"], "kernel"),
        ("dispatcher, one-task graph", mail["kdsbench.one"], "dispatcher"),
        ("dispatcher, chain task", mail["kdsbench.chain"], "dispatcher"),
        ("dispatcher, independent task", mail["kdsbench.wide"], "dispatcher"),
        ("dispatcher, CPU task", mail["kdsbench.cpu"], "dispatcher"),
        ("dispatcher, chain step", mail["kdschain.cycles_per_step"], "dispatcher"),
        ("dispatcher, MG round, mailbox", round_mail, "dispatcher"),
        ("dispatcher, MG round, engine", round_engine, "dispatcher"),
    ]


COLORS = {
    "firmware": SERIES[0],
    "kernel": SERIES[1],
    "dispatcher": SERIES[2],
}


def launches() -> list[tuple[str, int, int]]:
    with (TABLES / "zoo.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [
        (f"{r['model']} {r['mode']}", int(r["launches"]), int(r["cycles"]))
        for r in rows
        if r["mode"] == "z"
    ]


def paths() -> None:
    order = sorted(path_rows(), key=lambda p: p[1])
    fig, ax = plt.subplots(figsize=(11, 4.4))
    for label, cycles, source in order:
        bar = ax.barh(label, cycles, color=COLORS[source], label=source)
        ax.bar_label(bar, [f" {cycles:,}"], color=SECOND, fontsize=8)
    handles, labels = ax.get_legend_handles_labels()
    keep = dict(zip(labels, handles, strict=True))
    ax.legend(keep.values(), keep.keys(), loc="lower right")
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node cycles a launch")
    ax.set_title("One launch on each dispatch path")
    save(fig, "firmware", "dispatch_paths")


def projection() -> None:
    """Launches a step times a path's cost, against the step measured."""
    rows = sorted(launches(), key=lambda r: r[2])
    mail, engine = measures("t6low.all"), measures("t6low.engine")
    chosen = (
        (firmware_launch(), "firmware"),
        (engine["mg.engine_round_cycles"], "kernel engine"),
        (mail["kdsbench.chain"], "chain task"),
        (mail["kdsbench.one"], "one-task graph"),
    )
    fig, ax = plt.subplots(figsize=(11, 0.5 * len(rows) + 1.6))
    height = 0.8 / len(chosen)
    for at, (cost, name) in enumerate(chosen):
        ax.barh(
            [i + at * height for i in range(len(rows))],
            [n * cost / c for _, n, c in rows],
            height=height,
            color=SERIES[at],
            label=name,
        )
    ax.set_yticks([i + 0.3 for i in range(len(rows))], [r[0] for r in rows])
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("launch cost over the step's measured run cycles; a projection")
    ax.legend(loc="lower right")
    ax.set_title("Dispatch cost a step, projected from launches a step")
    save(fig, "firmware", "dispatch_projection")


def kernel_stages() -> None:
    """A system call's cycles by stage of the trap path, stacked per call kind."""
    prof = measures("t6low.prof")
    fig, ax = plt.subplots(figsize=(11, 3.2))
    left = dict.fromkeys(CALLS, 0)
    stacked = []
    for at, stage in enumerate(STAGES):
        cycles = [prof.get(f"kprof.{call}.{stage}", 0) for call in CALLS]
        bars = ax.barh(
            CALLS,
            cycles,
            left=[left[c] for c in CALLS],
            color=SERIES[at % len(SERIES)],
            label=stage,
        )
        stacked.append(bars)
        for call, v in zip(CALLS, cycles, strict=True):
            left[call] += v
    ax.bar_label(stacked[-1], [f" {left[c]:,}" for c in CALLS], color=SECOND)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node cycles a call")
    ax.legend(ncol=len(STAGES), loc="upper center", bbox_to_anchor=(0.5, -0.25))
    ax.set_title("A system call, by stage of the trap path")
    save(fig, "firmware", "os_kernel_stages")


def primitives() -> None:
    bench = measures("t6low.all")
    rows = sorted(((n, bench[k]) for k, n in PRIMITIVES), key=lambda r: r[1])
    fig, ax = plt.subplots(figsize=(11, 3.2))
    bars = ax.barh([r[0] for r in rows], [r[1] for r in rows], color=SERIES[1])
    ax.set_xscale("log")
    ax.bar_label(bars, [f" {cycles:,}" for _, cycles in rows], color=SECOND)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("node cycles, log scale")
    ax.set_title("OS primitives")
    save(fig, "firmware", "os_primitives")


def step(run: str) -> dict:
    """The last, warm step of a replay on the card."""
    doc = json.loads((DATA / "os" / run / "result.json").read_text(encoding="utf-8"))
    return doc["results"][-1]


def replay_paths() -> None:
    """The same step on every path, ViT and ViT-QKV side by side."""
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6), sharey=True)
    for ax, (capture, name) in zip(axes, CAPTURES, strict=True):
        base = step(f"rp.{capture}.fw")["cycles"]
        rows = [(label, step(f"rp.{capture}.{run}")["cycles"]) for run, label in REPLAY]
        for at, (label, cycles) in enumerate(rows):
            ax.barh(at, cycles, color=SERIES[0 if at < 2 else 1 if at < 4 else 2])
            ax.text(
                cycles,
                at,
                f" {cycles / 1e3:,.0f} k, {cycles / base:.2f}×",
                va="center",
                color=SECOND,
                fontsize=8,
            )
        ax.set_yticks(range(len(rows)), [r[0] for r in rows])
        ax.grid(axis="y", visible=False)
        ax.set_xlim(0, max(r[1] for r in rows) * 1.35)
        ax.xaxis.set_major_formatter(lambda v, _: f"{v / 1e3:,.0f} k")
        ax.set_xlabel("node cycles, warm step")
        ax.set_title(name)
    save(fig, "firmware", "replay_paths")


def replay_clocks(board: int, captures: tuple, out: str) -> None:
    """ViT and ViT-QKV on one board at each clock, every path."""
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6), sharey=True)
    runs = [r for r in REPLAY if r[0] != "fwl"]
    height = 0.8 / len(CLOCKS)
    for ax, (capture, name) in zip(axes, captures, strict=True):
        for i, (label, *tags) in enumerate(CLOCKS):
            cycles = [
                step(f"{tags[board]}.{capture}.{run}")["cycles"] for run, _ in runs
            ]
            ax.barh(
                [a + i * height for a in range(len(runs))],
                cycles,
                height=height,
                color=SERIES[i],
                label=label,
            )
        ax.set_yticks([a + height for a in range(len(runs))], [r[1] for r in runs])
        ax.xaxis.set_major_formatter(lambda v, _: f"{v / 1e3:,.0f} k")
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("node cycles, warm step")
        ax.set_title(name)
    axes[1].legend(loc="lower right")
    save(fig, "firmware", out)


def replay_seam() -> None:
    """ViT on the OS path, launch by launch: kernel and the time to the next one."""
    stamps = [s[0] for s in step("rp.vit.os")["stamps"]]
    runs = [b - a for a, b in zip(stamps[::2], stamps[1::2], strict=True)]
    seams = [b - a for a, b in zip(stamps[1::2], stamps[2::2], strict=False)] + [0]
    fig, ax = plt.subplots(figsize=(11, 3.2))
    at = range(len(runs))
    ax.bar(at, runs, color=SERIES[0], label="stamp to stamp, kernel")
    ax.bar(at, seams, bottom=runs, color=SERIES[1], label="seam to the next launch")
    ax.grid(axis="x", visible=False)
    ax.set_xlabel("launch")
    ax.set_ylabel("node cycles, stamped")
    ax.legend(loc="upper right")
    ax.set_title("ViT on the OS path, each launch and its seam")
    save(fig, "firmware", "replay_seam")


def four_dies() -> None:
    """Across four dies at mid: what each operation costs, near against far, and the
    workloads' speedup on four workers."""
    near = measures("t8mid.4n")
    work = measures("t8mid.4nw")
    low = measures("t8low.4n")
    fig, axes = plt.subplots(1, 2, figsize=(12, 3.8), width_ratios=(2, 1))
    rows = sorted(((n, near[k]) for k, n in FAR), key=lambda r: r[1])
    axes[0].barh([r[0] for r in rows], [r[1] for r in rows], color=SERIES[1])
    for name, cycles in rows:
        axes[0].text(cycles, name, f" {cycles:,}", va="center", color=SECOND)
    axes[0].grid(axis="y", visible=False)
    axes[0].set_xlabel("node cycles")
    axes[0].set_title("Four dies, 24 units, at mid")
    for at, (clock, run) in enumerate((("low", low), ("mid", work))):
        gains = [run[f"{k}.speedup_x100"] / 100 for k in ("preduce", "pmatmul")]
        x = [i + 0.4 * at for i in range(2)]
        axes[1].bar(x, gains, width=0.38, color=SERIES[at], label=clock)
        for i, gain in zip(x, gains, strict=True):
            axes[1].text(
                i, gain, f"{gain:.2f}×", ha="center", va="bottom", color=SECOND
            )
    axes[1].set_xticks([0.2, 1.2], ["reduce", "CPU matmul"])
    axes[1].axhline(4, color=SECOND, linewidth=1)
    axes[1].legend(ncol=2, loc="upper center", bbox_to_anchor=(0.5, -0.08))
    axes[1].grid(axis="x", visible=False)
    axes[1].set_ylabel("speedup on four workers")
    axes[1].set_title("Line at 4: linear")
    save(fig, "firmware", "os_four_dies")


def clock_effect() -> None:
    """Cycles at mid over cycles at low: 1 is pure core work, above is memory."""
    low, mid = measures("t6low.all"), measures("t6mid.all")
    rows = sorted(
        ((name, mid[key] / low[key]) for key, name in CLOCKED), key=lambda r: r[1]
    )
    fig, ax = plt.subplots(figsize=(11, 3.2))
    bars = ax.barh([r[0] for r in rows], [r[1] for r in rows], color=SERIES[1])
    ax.axvline(1, color=SECOND, linewidth=1)
    ax.bar_label(bars, [f" {ratio:.2f}" for _, ratio in rows], color=SECOND)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("cycles at 200 MHz over cycles at 100 MHz")
    ax.set_title("OS operations as the clock doubles and DDR4 does not")
    save(fig, "firmware", "os_clock")


def main():
    paths()
    projection()
    kernel_stages()
    primitives()
    replay_paths()
    replay_clocks(0, CAPTURES, "replay_paths_22")
    replay_clocks(1, CAPTURES_42, "replay_paths_42")
    replay_seam()
    four_dies()
    clock_effect()


if __name__ == "__main__":
    main()
