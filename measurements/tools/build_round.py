"""Every table, figure and comparison of a round, once `data/` holds its packed runs, and
the round's index. A step that fails is named at the end; the others still run, so one
pass reports everything.

    MEASURE_ROUND=c1 python measurements/tools/build_round.py
"""

import concurrent.futures
import csv
import json
import pathlib
import subprocess
import sys

import matplotlib.pyplot as plt
from build_index import RUNS, matrix_index
from runs import names, run_name
from style import DATA, ROUND, TABLES, save
from traces import json_lines, steady_summary

TOOLS = pathlib.Path(__file__).parent
WORKERS = 4
MODELS = ("lenet", "vit", "vitb", "mixer", "gru")
#: Figures of one run's hosts: (script, model, mode).
PATHS = (
    ("fig_path.py", "lenet", "zp"),
    ("fig_path.py", "vit", "z"),
    ("fig_path.py", "vitb", "z"),
    ("fig_diagram.py", "lenet", "zp"),
)


def step(script: str, *args: str) -> str | None:
    """The step's failure, or None."""
    got = subprocess.run(
        [sys.executable, str(TOOLS / script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=TOOLS,
        check=False,
    )
    print(script, *args, "failed" if got.returncode else "ok", flush=True)
    return f"{script} {' '.join(args)}\n{got.stderr[-600:]}" if got.returncode else None


def per_run() -> list[tuple[str, ...]]:
    """Each step that reads one named run, for the runs this round has."""
    have = set(names())
    out = []
    for run in RUNS:
        model, zoo_mode = run.split(".")
        name = run_name(model, zoo_mode, "multimesh_v9t6")
        if name in have:
            out.append(("fig_run.py", name))
    for model in MODELS:
        name = run_name(model, "z", "multimesh_v9t6")
        if name in have:
            out.append(("fig_model.py", name))
    for script, model, zoo_mode in PATHS:
        name = run_name(model, zoo_mode, "multimesh_v9t6")
        if name in have:
            out.append((script, name))
    for path in sorted((TABLES / "steps").glob("*.json")):
        out.append(("fig_steps.py", path.stem))
    return out


def to_examine() -> list[str]:
    """Runs with no output statistics, or with values further from f32 than the
    simulated MXFP7 run's largest error."""
    with (TABLES / "zoo.csv").open(encoding="utf-8", newline="") as f:
        return [
            f"{row['run']}: "
            + (
                f"{row['outside_footprint']} values outside the MXFP7 footprint "
                f"(abs max {row['card_f32_abs_max']} against "
                f"{row['mxfp7_f32_abs_max']})"
                if row["outside_footprint"]
                else "no output statistics"
            )
            for row in csv.DictReader(f)
            if row["outside_footprint"] != "0"
        ]


def matrix_runs():
    """Schema-2 rounds are discovered from metadata, not zoo tag suffixes."""
    result = []
    for path in sorted(DATA.glob("*/meta.json")):
        metadata = json.loads(path.read_text("utf-8"))
        if metadata.get("schema", 0) >= 2 and metadata.get("round") == ROUND:
            result.append((path.parent, metadata))
    return result


def matrix_report(found):
    """All boards, batches, participant counts and paths, including failed points.

    Compare only like workload/clock/tree/batch/block/group configurations. The
    local-max sum and global input bound have separate columns and speedups.
    Per-kind cycles are measured launch kinds: a combined graph stays 'graph'.
    """
    TABLES.mkdir(parents=True, exist_ok=True)
    rows, kinds = [], []
    for directory, metadata in found:
        post_path = directory / "post.json"
        post = json.loads(post_path.read_text("utf-8")) if post_path.exists() else {}
        base = {
            "run": directory.name,
            "workload": metadata["workload"],
            "board": metadata["board"],
            "batch": metadata["batch"],
            "sysnodes": metadata["nodes"],
            "path": metadata["path"],
            "blocks": metadata.get("blocks"),
            "commit": metadata.get("commit"),
            "clocks": json.dumps(metadata.get("clocks"), sort_keys=True),
            "profile": metadata.get("profile"),
            "status": "complete",
            "reason": "",
            "group": "",
            "samples": 0,
            "replay_inputs": 0,
            "launch_max_sum": None,
            "global_cycles_bound": None,
            "global_host_bound_ns": None,
            "model_cycles": None,
            "kinds": "",
            "p1_speedup": None,
            "p1_global_speedup": None,
            "eager_speedup": None,
            "eager_global_speedup": None,
        }
        try:
            if metadata.get("done") != "exited" or not post.get("complete"):
                raise ValueError(
                    metadata.get("done", "unfinished")
                    + "; "
                    + "; ".join(post.get("errors", ["postprocessing incomplete"]))
                )
            if not metadata.get("commit") or not metadata.get("clocks"):
                raise ValueError("missing provenance")
            summary = steady_summary(directory, metadata)
            if not summary["groups"]:
                raise ValueError("no selected replay/steady inputs")
            timed = {
                r["name"]: r
                for r in json_lines(directory / f"timed.{metadata['board']}.jsonl")
            }
            launches = json_lines(directory / "launches.jsonl")
            for group, values in summary["groups"].items():
                selected = [
                    e
                    for e in launches
                    if e.get("input") == values["representative_input"]
                    and e.get("phase") != "replay"
                ]
                model = sum(timed[e["name"]]["cycles"] for e in selected)
                row = dict(
                    base,
                    group=group,
                    samples=values["samples"],
                    replay_inputs=summary["replay_inputs"],
                    launch_max_sum=values["launch_max_sum"],
                    global_cycles_bound=values["global_cycles_bound"],
                    global_host_bound_ns=values["global_host_bound_ns"],
                    model_cycles=model,
                    kinds=json.dumps(values["kinds"], sort_keys=True),
                )
                rows.append(row)
                for kind, cycles in values["kinds"].items():
                    kinds.append(
                        {
                            "run": directory.name,
                            "board": row["board"],
                            "batch": row["batch"],
                            "sysnodes": row["sysnodes"],
                            "path": row["path"],
                            "group": group,
                            "kind": kind,
                            "cycles": cycles,
                            "share": cycles / values["launch_max_sum"]
                            if values["launch_max_sum"]
                            else 0,
                            "evidence": "measured",
                        }
                    )
        except (ValueError, KeyError) as error:
            rows.append(dict(base, status="unscored", reason=str(error)))

    def comparable(row, *, path=True, nodes=True):
        keys = (
            "workload",
            "board",
            "batch",
            "blocks",
            "group",
            "commit",
            "clocks",
            "profile",
        )
        keys += ("path",) if path else ()
        keys += ("sysnodes",) if nodes else ()
        return tuple(row[k] for k in keys)

    complete = [r for r in rows if r["status"] == "complete"]
    for row in complete:
        p1 = [
            r
            for r in complete
            if r["sysnodes"] == 1
            and comparable(r, nodes=False) == comparable(row, nodes=False)
        ]
        eager = [
            r
            for r in complete
            if r["path"] == "eager"
            and comparable(r, path=False) == comparable(row, path=False)
        ]
        for candidates, prefix in ((p1, "p1"), (eager, "eager")):
            if len(candidates) == 1:
                reference = candidates[0]
                if row["launch_max_sum"]:
                    row[f"{prefix}_speedup"] = (
                        reference["launch_max_sum"] / row["launch_max_sum"]
                    )
                # The same host-bound definition is available for both backends.
                if row["global_host_bound_ns"]:
                    row[f"{prefix}_global_speedup"] = (
                        reference["global_host_bound_ns"] / row["global_host_bound_ns"]
                    )
    for filename, entries, fields in (
        ("matrix.csv", rows, list(rows[0])),
        (
            "matrix-kinds.csv",
            kinds,
            [
                "run",
                "board",
                "batch",
                "sysnodes",
                "path",
                "group",
                "kind",
                "cycles",
                "share",
                "evidence",
            ],
        ),
    ):
        with (TABLES / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(entries)
    if complete:
        labels = [
            f"{r['workload']} {r['board'].removeprefix('multimesh_')} B{r['batch']} P{r['sysnodes']} {r['path']} {r['group']}"
            for r in complete
        ]
        fig, axes = plt.subplots(
            1, 2, figsize=(16, max(3, len(complete) * 0.35)), layout="constrained"
        )
        axes[0].barh(labels, [r["launch_max_sum"] / 1e6 for r in complete])
        axes[0].set_xlabel("sum of launch max-node durations (M cycles)")
        axes[1].barh(labels, [r["global_host_bound_ns"] / 1e6 for r in complete])
        axes[1].set_xlabel("global input enclosing host bound (ms)")
        for axis in axes:
            axis.invert_yaxis()
        save(fig, "matrix", "intervals")
        fig, ax = plt.subplots(figsize=(12, max(3, len(complete) * 0.35)))
        bottoms = [0.0] * len(complete)
        decoded = [json.loads(r["kinds"]) for r in complete]
        for kind in sorted({k for values in decoded for k in values}):
            values = [v.get(kind, 0) / 1e6 for v in decoded]
            ax.barh(labels, values, left=bottoms, label=kind)
            bottoms = [a + b for a, b in zip(bottoms, values, strict=True)]
        ax.invert_yaxis()
        ax.legend()
        ax.set_xlabel(
            "measured launch kinds (M cycles); retained combined regions are graph"
        )
        save(fig, "matrix", "kinds")
    matrix_index(rows)
    return int(any(r["status"] != "complete" for r in rows))


def main():
    found = matrix_runs()
    if found:
        return matrix_report(found)
    failed = [step("fig_zoo.py")]
    wide = [
        "fig_scale.py",
        "fig_waste.py",
        "fig_os.py",
        "fig_memory.py",
        "fig_units.py",
    ]
    wide += ["fig_compile.py"]
    wide += [] if ROUND == "z" else ["fig_compare.py"]
    steps = [(s,) for s in wide] + per_run()
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        failed += pool.map(lambda s: step(*s), steps)
    failed.append(step("build_index.py"))
    failed = [f for f in failed if f]
    for f in failed:
        print("---", f)
    print(len(steps) + 2 - len(failed), "steps ok,", len(failed), "failed")
    examine = to_examine()
    for e in examine:
        print("---", e)
    print(len(examine), "runs with outputs to examine")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
