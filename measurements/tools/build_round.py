"""Every table, figure and comparison of a round, once `data/` holds its packed runs, and
the round's index. A step that fails is named at the end; the others still run, so one
pass reports everything.

    MEASURE_ROUND=c1 python measurements/tools/build_round.py
"""

import concurrent.futures
import pathlib
import subprocess
import sys

from build_index import RUNS
from runs import names, run_name
from style import ROUND, TABLES

TOOLS = pathlib.Path(__file__).parent
WORKERS = 4
MODELS = ("lenet", "vit", "mixer", "gru")
#: Figures of one run's hosts: (script, model, mode).
PATHS = (
    ("fig_path.py", "lenet", "zp"),
    ("fig_path.py", "vit", "z"),
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


def main():
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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
