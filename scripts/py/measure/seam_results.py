"""Summarize saved depth-one profiles and launch records without rerunning them."""

import json
from pathlib import Path

from seam_depth1 import category

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "build/seam/depth1"


def main():
    result = {}
    for path in sorted(OUT.glob("*-profile.json")):
        data = json.loads(path.read_text())
        costs = {}
        for row in data["profile"]:
            cat = category(row)
            costs[cat] = costs.get(cat, 0) + row["exclusive_seconds"]
        n = data["steps"][-1]["launches"]
        result[path.stem] = {
            "launches": n,
            "exclusive_ms_per_launch": {k: v * 1000 / n for k, v in costs.items()},
        }
    for path in sorted(OUT.glob("*.json")):
        if (
            "profile" in path.name
            or "summary" in path.name
            or path.name == "costs.json"
        ):
            continue
        data = json.loads(path.read_text())
        if "steps" in data:
            result[path.stem] = {
                "warm_ms_per_launch": [r["ms_per_launch"] for r in data["steps"][4:]],
                "traffic": data["steps"][-1]["traffic"],
            }
    (OUT / "costs.json").write_text(json.dumps(result, indent=2) + "\n")
    for key, value in result.items():
        print(
            key, value.get("exclusive_ms_per_launch", value.get("warm_ms_per_launch"))
        )


if __name__ == "__main__":
    main()
