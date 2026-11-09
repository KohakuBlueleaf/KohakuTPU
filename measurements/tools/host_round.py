"""Post a round: each trace's own machine, actual retained kernels, detectors, pack.

Failures are packed too. Missing provenance, missing replays, unmatched timed
kernels and postprocessing failures are explicit, never a zero-cost success.
This tool neither builds nor accesses a card.
"""

import argparse
import concurrent.futures
import csv
import json
import os
import pathlib
import subprocess
import sys
import traceback

from pack import pack

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "measurements/tools"
PERF = REPO / "software/tests/perf"
TIMED = REPO / "software/target/stage/timed"
WORKERS = 4
sys.path.insert(0, str(PERF))
from traces import json_lines, steady_summary


def run(*command):
    # Four traces at once, one timed-model worker each: never 4 x CPU-count.
    return subprocess.run(
        [str(c) for c in command],
        check=True,
        capture_output=True,
        cwd=REPO,
        env=dict(os.environ, RAYON_NUM_THREADS="1"),
        timeout=600,
    )


def traced(trace):
    return any("trace" in row for row in json_lines(trace / "card.jsonl"))


def discover(traces, round_tag):
    found = []
    for attempt in traces.rglob("attempt.json"):
        directory = attempt.parent
        if (directory / "meta.json").exists():
            continue
        provenance = directory / "provenance.json"
        state = json.loads(attempt.read_text("utf-8"))
        point = state["point"]
        fallback = (
            json.loads(provenance.read_text("utf-8"))
            if provenance.exists()
            else {
                "schema": 2,
                "round": round_tag,
                "workload": point["workload"],
                "board": point["board"],
                "nodes": point["sysnodes"],
                "batch": point["batch"],
                "blocks": point["blocks"],
                "path": point["path"],
                "args": [],
                "commit": None,
                "clocks": None,
            }
        )
        fallback["done"] = "unfinished attempt; card configuration is never rerun"
        (directory / "meta.json").write_text(json.dumps(fallback, indent=1), "utf-8")
        if not (directory / "failure.txt").exists():
            (directory / "failure.txt").write_text(fallback["done"], "utf-8")
        if not (directory / "klog.json").exists():
            (directory / "klog.json").write_text(
                json.dumps({"status": "unavailable after interruption"}), "utf-8"
            )
    for path in sorted(traces.rglob("meta.json")):
        if "preflight" in path.relative_to(traces).parts:
            continue
        metadata = json.loads(path.read_text("utf-8"))
        if metadata.get("backend") == "offline":
            continue  # Library fills belong in compile.csv, not card tables.
        if metadata.get("schema", 0) < 2 and not (path.parent / "card.jsonl").exists():
            continue
        if metadata.get("round") == round_tag or (
            not metadata.get("round")
            and path.parent.name.partition(".")[2].startswith(round_tag)
        ):
            found.append(path.parent)
    return found


def post(trace, out, tables):
    metadata = json.loads((trace / "meta.json").read_text("utf-8"))
    machine = metadata["board"]
    errors = []
    status = {"schema": 2, "machine": machine, "complete": False, "errors": errors}
    try:
        if metadata.get("schema", 0) >= 2 and (
            not metadata.get("commit") or not metadata.get("clocks")
        ):
            raise ValueError("round trace lacks synced commit or read-clocks")
        attempt_path = trace / "attempt.json"
        attempt = (
            json.loads(attempt_path.read_text("utf-8")) if attempt_path.exists() else {}
        )
        if attempt and attempt.get("status") != "complete":
            raise ValueError(
                f"driver attempt is {attempt.get('status')}; not a scored run"
            )
        if metadata.get("done") != "exited" or (trace / "failure.txt").exists():
            raise ValueError(
                "failed/interrupted card attempt; retained as evidence, not a timing row"
            )
        timed = trace / f"timed.{machine}.jsonl"
        run(
            TIMED,
            "--card",
            "--machine",
            REPO / "machines" / f"{machine}.json",
            "--params",
            PERF / "params.json",
            "--out",
            timed,
            trace / "kernels",
        )
        modeled = json_lines(timed)
        launches = json_lines(trace / "launches.jsonl")
        if metadata.get("schema", 0) >= 2:
            steady = steady_summary(trace, metadata)
            if not steady["samples"]:
                raise ValueError(
                    "no complete selected steady inputs (capture is not replay)"
                )
            (trace / "steady.json").write_text(json.dumps(steady, indent=1), "utf-8")
            chosen = set(steady["selected_inputs"])
            expected = {
                e["name"]
                for e in launches
                if e.get("phase") != "replay" and e.get("input") in chosen
            }
        else:
            expected = {e["name"] for e in launches if e.get("phase") != "replay"}
        status["nonselected_model_errors"] = [
            r for r in modeled if "error" in r and r["name"] not in expected
        ]
        valid = {r["name"] for r in modeled if "error" not in r}
        missing = expected - valid
        if missing:
            raise ValueError(f"unmodeled selected launched kernels: {sorted(missing)}")
        run(
            sys.executable,
            PERF / "waste.py",
            trace,
            "--machine",
            machine,
            "--out",
            trace / "waste.json",
        )
        if traced(trace):
            path = tables / "steps" / f"{trace.name}.json"
            run(sys.executable, TOOLS / "steps.py", trace, path)
        status["complete"] = True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        errors.append(traceback.format_exc())
    (trace / "post.json").write_text(json.dumps(status, indent=1), "utf-8")
    # Packing is independent of the model/detector outcome. It also keeps logs
    # when no card.jsonl was produced (boot, preflight, timeout, join failure).
    pack(trace, out / trace.name)
    return trace.name, status["complete"]


def compile_table(traces, round_tag, tables):
    rows = []
    for path in sorted(traces.rglob("compiles.jsonl")):
        if f".{round_tag}f" not in path.parent.name:
            continue
        times = [r["seconds"] for r in json_lines(path)]
        rows.append(
            {
                "run": path.parent.name,
                "kernels": len(times),
                "seconds": sum(times),
                "slowest": max(times, default=0),
            }
        )
    with (tables / "compile.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["run", "kernels", "seconds", "slowest"]
        )
        writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("round")
    ap.add_argument("root", type=pathlib.Path)
    ap.add_argument("--traces", type=pathlib.Path, default=REPO / "build/perf/traces")
    a = ap.parse_args()
    tables = a.root / "rounds" / a.round / "data"
    (tables / "steps").mkdir(parents=True, exist_ok=True)
    traces = discover(a.traces, a.round)
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        jobs = [pool.submit(post, t, a.root / "data", tables) for t in traces]
        for job in concurrent.futures.as_completed(jobs):
            name, complete = job.result()
            failed += not complete
            print(name, "posted" if complete else "packed with failure", flush=True)
    compile_table(a.traces, a.round, tables)
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(main())
