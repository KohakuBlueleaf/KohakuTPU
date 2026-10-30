"""Per-rule performance mutants: restore exactly one elided copy boundary."""

import argparse
import json
import runpy
import subprocess
import sys
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

from kohakutpu.application.ktpugrad import launch_rules
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM

ROOT = Path(__file__).resolve().parents[3]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rule", choices=("all", "none", *launch_rules.RULES))
    args = ap.parse_args()
    if args.rule == "all":
        for rule in ("none", *launch_rules.RULES):
            result = subprocess.run(
                [sys.executable, __file__, rule],
                capture_output=True,
                text=True,
                check=False,
                timeout=115,
            )
            print(
                rule,
                result.returncode,
                result.stdout.splitlines()[-1:] or result.stderr[-300:],
                flush=True,
            )
        return
    # Isolate copy elision from the independent consumer-prologue fusion.
    FM.PREPARED = False
    dev = D.attach(D.Device("offline", board="multimesh_v9t6"))
    helper = runpy.run_path(str(ROOT / "scripts/py/templates/prebuild.py"))
    try:
        with (
            R.knobs(elide_contiguous=True),
            launch_rules.keep_copy(args.rule) if args.rule != "none" else nullcontext(),
        ):
            captured = helper["capture"](dev, 8, False, "lenet")
        dest = ROOT / ".kt/launch-loop2/copy-rules"
        dest.mkdir(parents=True, exist_ok=True)
        output = {
            "rule": args.rule,
            "runs": len(captured["launches"]),
            "kinds": dict(
                Counter(captured["sources"][k]["family"] for k in captured["launches"])
            ),
        }
        (dest / f"{args.rule}.json").write_text(
            json.dumps(output, indent=2), encoding="utf-8"
        )
        print(json.dumps(output))
    finally:
        dev.close()


if __name__ == "__main__":
    main()
