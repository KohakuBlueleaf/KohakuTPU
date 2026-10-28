"""Summarize HSEAM protocol counts and compare saved model output bytes."""

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
WORK = ROOT / ".plan" / "hseam"


def main():
    summary = {"counting": {}, "model_parity": {}}
    for path in sorted((WORK / "loop2").glob("*.json")):
        if path.name.startswith("summary"):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("steps"):
            continue
        row = data["steps"][-1]
        summary["counting"][path.stem] = {
            "status": data.get("status"),
            "calls": row["transport_calls"],
            "bytes": sum(v["bytes"] for v in row.get("traffic", {}).values()),
            "launches": row["launches"],
            "calls_per_launch": row["transport_calls"] / row["launches"],
            "seconds": row["seconds"],
            "ms_per_launch": row["ms_per_launch"],
            "traffic_per_launch": {
                k: {label: value / row["launches"] for label, value in v.items()}
                for k, v in row.get("traffic", {}).items()
            },
        }
    for workload, baseline in (
        ("lenet-eager", "model-lenet-sync"),
        ("vit-eager", "model-vit-sync"),
    ):
        path = WORK / "loop2" / f"model-{workload}.json"
        if not path.exists():
            continue
        new = json.loads(path.read_text(encoding="utf-8"))
        old = json.loads(
            (WORK / "before" / f"{baseline}.json").read_text(encoding="utf-8")
        )
        parity = []
        for a, b in zip(new["steps"], old["steps"], strict=True):
            count = np.asarray(a["output"]).size
            width = len(bytes.fromhex(a["output_hex"])) // count
            dtype = {2: np.float16, 4: np.float32}[width]
            same = (
                bytes.fromhex(a["output_hex"])
                == np.asarray(b["output"], dtype=dtype).tobytes()
            )
            parity.append(
                {
                    "step": a["step"],
                    "bit_identical": same,
                    "elements": count,
                    "new_cycles": a["counters"]["cycles"],
                    "old_cycles": b["counters"]["cycles"],
                    "new_idle": a["idle_cycles"],
                    "old_idle": b["idle_cycles"],
                }
            )
        summary["model_parity"][workload] = parity
    out = WORK / "loop2" / "summary.json"
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    for name, row in summary["counting"].items():
        print(name, row["calls"], row["launches"], round(row["seconds"], 4))
    print(json.dumps(summary["model_parity"], indent=2))


if __name__ == "__main__":
    main()
