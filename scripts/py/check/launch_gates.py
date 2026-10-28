"""Bounded offline gate groups for launch experiments (run on the Linux test host)."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--group", choices=("runtime", "api", "kernels", "ktpugrad"), required=True
    )
    ap.add_argument("--seconds", type=int, default=115)
    args = ap.parse_args()
    folder = ROOT / "software/application/tests" / args.group
    dest = ROOT / ".kt/launch-gates"
    dest.mkdir(parents=True, exist_ok=True)
    results = []
    files = sorted(folder.glob("test_*.py"))
    for start in range(0, len(files), 5):
        batch = files[start : start + 5]
        label = f"{args.group}-{start // 5}"
        command = [
            sys.executable,
            "-m",
            "pytest",
            *(str(p) for p in batch),
            "-q",
            "-n",
            "4",
            "-p",
            "no:cacheprovider",
        ]
        tick = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=ROOT / "software",
                timeout=args.seconds,
                check=False,
                capture_output=True,
                text=True,
            )
            output, status = result.stdout + result.stderr, result.returncode
        except subprocess.TimeoutExpired as error:
            output = (error.stdout or b"").decode(errors="replace") + (
                error.stderr or b""
            ).decode(errors="replace")
            status = "timeout"
        elapsed = time.monotonic() - tick
        (dest / f"{label}.log").write_text(output, encoding="utf-8")
        results.append(
            dict(
                label=label,
                status=status,
                seconds=elapsed,
                files=[p.name for p in batch],
                tail=output.splitlines()[-3:],
            )
        )
        print(
            label,
            status,
            round(elapsed, 1),
            " | ".join(output.splitlines()[-2:]),
            flush=True,
        )
    (dest / f"{args.group}.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
