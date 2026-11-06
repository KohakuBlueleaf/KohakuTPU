"""Offline F-014 reach census of compiled packages with concrete memory and binds.

Input: a JSON list of {package, bindings, memory, mesh} records. Paths are
relative to that JSON file; memory is a binary image starting at address zero.
Supply real index data for gathers. Address-sensitive results apply to these
bindings only. This does not launch a device or certify a program.

    python software/tests/perf/f014_reach.py samples.json --machine machines/board.json
"""

import argparse
import json
from pathlib import Path

from kohakutpu.stack.rs import _rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", type=Path)
    parser.add_argument("--machine", required=True, type=Path)
    args = parser.parse_args()
    machine = args.machine.read_text(encoding="utf-8")
    samples = json.loads(args.samples.read_text(encoding="utf-8"))
    results = []
    counts = {}
    for sample in samples:
        package = args.samples.parent / sample["package"]
        memory = args.samples.parent / sample["memory"]
        result = json.loads(
            _rs.f014_reach(
                package.read_bytes(),
                sample["bindings"],
                machine,
                memory.read_bytes(),
                sample.get("mesh", 0),
            )
        )
        result["package"] = str(package)
        results.append(result)
        for kind, values in result["counts"].items():
            total = counts.setdefault(kind, [0, 0, 0])
            for i, value in enumerate(values):
                total[i] += value
    print(json.dumps({"counts": counts, "samples": results}, indent=2))
    return int(any(not result["complete"] for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
