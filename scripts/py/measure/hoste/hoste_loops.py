"""Measure each HOSTE mechanism in a foreground cumulative knob ladder."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# native recipe, eager schedule, eager tape, wire patch, native memory/program,
# compiled schedule, native queue. Adjacent rows isolate one mechanism.
CONFIGS = {
    "baseline": (0, 0, 0, 0, 0, 0, 0, 0),
    "schedule": (0, 1, 0, 0, 0, 0, 0, 0),
    "exec": (0, 1, 0, 0, 0, 0, 1, 0),
    "tape": (0, 1, 1, 0, 0, 0, 1, 0),
    "native": (1, 1, 1, 0, 0, 0, 1, 0),
    "wire": (1, 1, 1, 1, 0, 0, 1, 0),
    "memory": (1, 1, 1, 1, 1, 0, 1, 0),
    "program": (1, 1, 1, 1, 1, 1, 1, 0),
    "queue": (1, 1, 1, 1, 1, 1, 1, 1),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--steps", type=int, default=9)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    reports = []
    for backend in ("counting", "offline"):
        for workload in ("mlp", "lenet-eager", "vit-eager"):
            for name, knobs in CONFIGS.items():
                if backend == "offline" and name not in ("baseline", "program"):
                    continue
                native, schedule, tape, wire, memory, program, execution, queue = knobs
                env = os.environ | {
                    "KTPU_NATIVE_MEMORY": str(memory),
                    "KTPU_NATIVE_PROGRAM": str(program),
                    "KTPU_EAGER_EXEC": str(execution),
                    "KTPU_NATIVE_QUEUE": str(queue),
                }
                output = args.output / f"{workload}-{backend}-{name}.json"
                command = [
                    sys.executable,
                    str(Path(__file__).with_name("hoste.py")),
                    workload,
                    "--native",
                    str(native),
                    "--schedule",
                    str(schedule),
                    "--recipe",
                    str(tape),
                    "--wire",
                    str(wire),
                    "--backend",
                    backend,
                    "--steps",
                    str(args.steps),
                    "--output",
                    str(output),
                ]
                completed = subprocess.run(
                    command, env=env, capture_output=True, text=True, check=False
                )
                output.with_suffix(".log").write_text(
                    completed.stdout + completed.stderr, encoding="utf-8"
                )
                if completed.returncode:
                    raise RuntimeError(completed.stdout + completed.stderr)
                report = json.loads(output.read_text(encoding="utf-8"))
                report.pop("profile")
                report.pop("schedule_shapes")
                report["configuration"]["ladder"] = name
                report["configuration"]["knobs"] = knobs
                reports.append(report)
                (args.output / "summary.json").write_text(
                    json.dumps(reports, indent=2) + "\n", encoding="utf-8"
                )
                print(
                    json.dumps({k: v for k, v in report.items() if k != "samples"}),
                    flush=True,
                )


if __name__ == "__main__":
    main()
