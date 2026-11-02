"""One round's finished card traces into a measurements tree, on the host: the timed
model on the run's own machine, the detectors, step records of a traced run, then pack.
A trace reads as it stands; only `timed.*` and `waste.json` land beside its records.

    python measurements/tools/host_round.py ROUND MEASUREMENTS_DIR [--traces DIR]
"""

import argparse
import concurrent.futures
import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "measurements" / "tools"
PERF = REPO / "software" / "tests" / "perf"
TIMED = REPO / "software" / "target" / "stage" / "timed"
WORKERS = 4


def run(*command) -> None:
    subprocess.run([str(c) for c in command], check=True, capture_output=True, cwd=REPO)


def traced(trace: pathlib.Path) -> bool:
    """Whether the run booted the debug image: its rows carry step records."""
    with (trace / "card.jsonl").open(encoding="utf-8") as f:
        return "trace" in json.loads(f.readline())


def post(trace: pathlib.Path, out: pathlib.Path, tables: pathlib.Path) -> str:
    """One trace; a packed one stands, so a pass cut short goes on where it stopped."""
    if (out / trace.name / "meta.json").exists():
        return trace.name
    machine = trace.name.split(".")[-2]
    timed = trace / f"timed.{machine}.jsonl"
    params = PERF / "params.json"
    spec = REPO / "machines" / f"{machine}.json"
    run(
        TIMED,
        "--card",
        "--machine",
        spec,
        "--params",
        params,
        "--out",
        timed,
        trace / "kernels",
    )
    waste = trace / "waste.json"
    run(sys.executable, PERF / "waste.py", trace, "--machine", machine, "--out", waste)
    if traced(trace):
        path = tables / "steps" / f"{trace.name.partition('.')[0]}.json"
        run(sys.executable, TOOLS / "steps.py", trace, path)
    run(sys.executable, TOOLS / "pack.py", trace, out / trace.name)
    return trace.name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("round")
    ap.add_argument("root", type=pathlib.Path)
    ap.add_argument("--traces", type=pathlib.Path, default=REPO / "build/perf/traces")
    a = ap.parse_args()
    tables = a.root / "rounds" / a.round / "data"
    (tables / "steps").mkdir(parents=True, exist_ok=True)
    traces = [
        p.parent
        for p in a.traces.glob("*.multimesh_*/card.jsonl")
        if p.parent.name.partition(".")[2].startswith(a.round)
    ]
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        jobs = [pool.submit(post, t, a.root / "data", tables) for t in sorted(traces)]
        for job in concurrent.futures.as_completed(jobs):
            print(job.result(), flush=True)
    print(len(traces), "traces")


if __name__ == "__main__":
    main()
