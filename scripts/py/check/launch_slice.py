"""Retry a named gate slice with the same collection order on either job tree."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("slice", type=int)
    ap.add_argument("-n", type=int, default=4)
    args = ap.parse_args()
    root = Path.cwd().parent
    out = root / ".kt/launch-loop2"
    out.mkdir(parents=True, exist_ok=True)
    collect = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            args.file,
            "--collect-only",
            "-q",
            "-n",
            "0",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    ids = [
        s.strip()
        for s in collect.stdout.splitlines()
        if s.startswith("application/") and "::" in s
    ]
    ids = ids[args.slice * 20 : (args.slice + 1) * 20]
    assert ids, collect.stdout + collect.stderr
    rows = []

    def run(label, selected, workers):
        start = time.monotonic()
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    *selected,
                    "-q",
                    "-n",
                    str(workers),
                    "-p",
                    "no:cacheprovider",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=115,
            )
            status, text = result.returncode, result.stdout + result.stderr
        except subprocess.TimeoutExpired as e:
            status, text = "timeout", (e.stdout or b"").decode(errors="replace") + (
                e.stderr or b""
            ).decode(errors="replace")
        (out / f"{label}.log").write_text(text, encoding="utf-8")
        rows.append(
            dict(
                label=label,
                status=status,
                seconds=time.monotonic() - start,
                tests=selected,
            )
        )
        print(label, status, round(rows[-1]["seconds"], 2), flush=True)
        return status

    stem = f"{Path(args.file).stem}-{args.slice}"
    if run(stem, ids, args.n) != 0:
        for i, case in enumerate(ids):
            run(f"{stem}-case{i}", [case], 1)
    (out / f"{stem}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
