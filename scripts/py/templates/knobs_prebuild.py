"""Remeasure non-lanes class derivation on the current board schema."""

import argparse
import json
import sys
from pathlib import Path

import derive
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    doc = json.loads(args.capture.read_text(encoding="utf-8"))
    doc["machine_json"] = rs.machine_json(OfflineCard(doc["board"]).machine)
    args.output.mkdir(parents=True, exist_ok=True)
    capture = args.output / "current-machine-capture.json"
    capture.write_text(json.dumps(doc), encoding="utf-8")
    summary = {
        "command": "python " + " ".join(sys.argv),
        "machine_overlay": "current named board; selected generators retraced",
        "measurements": [],
    }
    for policy in ("exact", "pow2", "coarse"):
        for cores in ("all", "min"):
            output = args.output / f"{policy}-{cores}/results.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            derive.run(
                capture,
                output,
                policy,
                256,
                False,
                cores,
                cores,
                cores,
                cores,
                cores,
                cores,
                cores,
            )
            result = json.loads(output.read_text(encoding="utf-8"))
            summary["measurements"].append(
                {
                    "policy": policy,
                    "cores": cores,
                    "seconds": result["wall_seconds"],
                    "templates": len(result["artifacts"]),
                    "generators": result["generators"],
                    "ledger": str(output),
                }
            )
            (args.output / "summary.json").write_text(
                json.dumps(summary, indent=2), encoding="utf-8"
            )


if __name__ == "__main__":
    main()
