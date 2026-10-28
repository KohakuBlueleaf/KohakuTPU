"""One lead-authorized ktpugrad defect suite, retaining its full result."""

import argparse
import subprocess
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path, help="log file receiving the full result")
    args = ap.parse_args()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "application/tests/ktpugrad",
            "-q",
            "-n",
            "4",
            "-p",
            "no:cacheprovider",
            "--tb=line",
        ],
        capture_output=True,
        text=True,
    )
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.stdout + result.stderr)
    print("\n".join(result.stdout.splitlines()[-95:]))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
