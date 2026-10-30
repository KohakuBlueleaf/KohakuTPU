"""Foreground HOSTE knob matrix, retaining raw profiles beside a compact summary."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--steps", type=int, default=9)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    reports = []
    for backend in ("counting", "offline"):
        for work in ("mlp", "lenet-eager", "vit-eager"):
            for native, schedule in ((0, 0), (1, 0), (0, 1), (1, 1)):
                if backend == "offline" and native != schedule:
                    continue
                dest = args.output / f"{work}-{backend}-{native}{schedule}.json"
                command = [
                    sys.executable,
                    str(Path(__file__).with_name("hoste.py")),
                    work,
                    "--native",
                    str(native),
                    "--schedule",
                    str(schedule),
                    "--backend",
                    backend,
                    "--steps",
                    str(args.steps),
                    "--output",
                    str(dest),
                ]
                result = subprocess.run(
                    command, capture_output=True, text=True, check=False
                )
                dest.with_suffix(".log").write_text(
                    result.stdout + result.stderr, encoding="utf-8"
                )
                if result.returncode:
                    raise RuntimeError(result.stdout + result.stderr)
                report = json.loads(dest.read_text(encoding="utf-8"))
                report.pop("profile")
                report.pop("schedule_shapes")
                reports.append(report)
                print(
                    json.dumps({k: v for k, v in report.items() if k != "samples"}),
                    flush=True,
                )
                (args.output / "summary.json").write_text(
                    json.dumps(reports, indent=2) + "\n", encoding="utf-8"
                )


if __name__ == "__main__":
    main()
