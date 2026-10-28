"""Golden shards or bounded per-file pytest slices on the Linux host."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("golden", "pytest"))
    ap.add_argument("-n", type=int, default=4)
    args = ap.parse_args()
    dest = ROOT / ".kt/launch-shards"
    dest.mkdir(parents=True, exist_ok=True)
    if args.mode == "golden":
        jobs = [
            (
                f"golden-{i}",
                [
                    "cargo",
                    "test",
                    "-p",
                    "ktpu",
                    "--features",
                    "mutants",
                    "--test",
                    "golden",
                    "the_stack_agrees_with_the_python_goldens",
                    "--",
                    "--exact",
                ],
                {"KTPU_GOLDEN_SHARD": f"{i}/16"},
            )
            for i in range(16)
        ]
        cwd = ROOT / "software/rust"
        seconds = 1800
    else:
        files = [
            "kernels/test_vector.py",
            "kernels/test_vit_model.py",
            "kernels/test_vit_rows.py",
            "ktpugrad/test_compile.py",
            "ktpugrad/test_check_numerical_edges.py",
            "ktpugrad/test_fragmented_jit.py",
        ]
        jobs = []
        cwd = ROOT / "software"
        seconds = 115
        for file in files:
            collect = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    f"application/tests/{file}",
                    "--collect-only",
                    "-q",
                    "-n",
                    "0",
                    "-p",
                    "no:cacheprovider",
                ],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            ids = [
                s.strip()
                for s in collect.stdout.splitlines()
                if s.startswith("application/") and "::" in s
            ]
            for i in range(0, len(ids), 20):
                jobs.append(
                    (
                        f"{Path(file).stem}-{i//20}",
                        [
                            sys.executable,
                            "-m",
                            "pytest",
                            *ids[i : i + 20],
                            "-q",
                            "-n",
                            "4",
                            "-p",
                            "no:cacheprovider",
                        ],
                        {},
                    )
                )
    rows = []
    for name, command, extra in jobs:
        env = dict(os.environ, CARGO_TARGET_DIR="target/job", **extra)
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=seconds,
            )
            status, output = result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired as e:
            status, output = "timeout", (e.stdout or b"").decode(errors="replace") + (
                e.stderr or b""
            ).decode(errors="replace")
        (dest / f"{name}.log").write_text(output, encoding="utf-8")
        rows.append(dict(name=name, status=status, tail=output.splitlines()[-3:]))
        (dest / f"{args.mode}.json").write_text(
            json.dumps(rows, indent=2), encoding="utf-8"
        )
        print(name, status, flush=True)


if __name__ == "__main__":
    main()
