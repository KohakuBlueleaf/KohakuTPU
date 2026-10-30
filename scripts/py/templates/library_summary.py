"""Summarize recorded exact-library measurements without claiming class reuse."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "lib",
        type=Path,
        help="library measurement tree: reads exact, vit-exact, model-forward; "
        "writes measure.json",
    )
    args = parser.parse_args()
    rows = []
    for name in ("exact", "vit-exact", "model-forward"):
        folder = args.lib / name
        doc = json.loads((folder / "prebuild.json").read_text(encoding="utf-8"))
        cap = json.loads((folder / "capture.json").read_text(encoding="utf-8"))
        for zero_sign, value in doc["zero_sign"].items():
            rows.append(
                {
                    "workload": name,
                    "zero_sign": zero_sign,
                    "batch": doc["batch"],
                    "board": doc["board"],
                    "launches": len(cap["launches"]),
                    "exact_classes": len(value["results"]),
                    "artifact_bytes": sum(
                        r["bytes"] for r in value["results"].values()
                    ),
                    "prebuild_seconds": value["seconds"],
                    "prebuild_hits": sum(r["hit"] for r in value["results"].values()),
                    "failures": len(value["failures"]),
                }
            )
    output = {
        "status": "partial; policy and count-path sweeps not measured",
        "exact_libraries": rows,
    }
    (args.lib / "measure.json").write_text(
        json.dumps(output, indent=2), encoding="utf-8"
    )
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
