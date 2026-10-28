"""Fresh offline prebuild/run/check evidence for both launch configurations."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
KNOBS = ("VIEW_FORMS", "ELIDE_CONTIGUOUS", "SINGLE_PACKAGE_AST", "HORIZONTAL_FUSION")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("off", "on"))
    args = ap.parse_args()
    env = dict(os.environ, **{f"KTPU_{k}": str(int(args.mode == "on")) for k in KNOBS})
    dest = ROOT / ".kt/launch-library-loop2" / args.mode
    library = ROOT / "build" / f"launch-library-{args.mode}"
    dest.mkdir(parents=True, exist_ok=True)
    results = []

    def invoke(label, command):
        start = time.monotonic()
        try:
            run = subprocess.run(
                [sys.executable, *command],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
            output, status = run.stdout + run.stderr, run.returncode
        except subprocess.TimeoutExpired as error:
            status = "timeout"
            output = (error.stdout or b"").decode(errors="replace") + (
                error.stderr or b""
            ).decode(errors="replace")
        (dest / f"{label}.log").write_text(output, encoding="utf-8")
        results.append(
            dict(label=label, status=status, seconds=time.monotonic() - start)
        )
        (dest / "summary.json").write_text(
            json.dumps(results, indent=2), encoding="utf-8"
        )
        print(label, status, round(results[-1]["seconds"], 2), flush=True)
        return status == 0

    for model, batch in (("lenet", 8), ("vit-tiny", 1)):
        base = [
            "scripts/py/templates/prebuild.py",
            model,
            "--batch",
            str(batch),
            "--board",
            "multimesh_v9t6",
            "--library",
            str(library),
            "--classes",
            "0",
        ]
        if not invoke(
            f"{model}-prebuild", base + ["--output", str(dest / f"{model}-prebuild")]
        ):
            continue
        invoke(
            f"{model}-eager",
            base
            + [
                "--mode",
                "run",
                "--steps",
                "3",
                "--output",
                str(dest / f"{model}-eager"),
            ],
        )
        invoke(
            f"{model}-jit",
            base
            + [
                "--mode",
                "run",
                "--steps",
                "6",
                "--jit",
                "--output",
                str(dest / f"{model}-jit"),
            ],
        )
    invoke(
        "step-checker",
        [
            "scripts/py/check/step_kernels.py",
            str(dest / "step-checker.json"),
            "--device",
            "offline",
            "--board",
            "multimesh_v9t6",
            "--library",
            str(library),
            "--batch",
            "8",
            "--steps",
            "1",
            "--check-step",
            "0",
            "--no-jit",
            "--data",
            "synthetic",
        ],
    )


if __name__ == "__main__":
    main()
