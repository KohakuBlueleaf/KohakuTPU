"""Reconcile the Loop7 executed census and summarize controlled experiments."""

import argparse
import json
import pickle
from collections import Counter
from pathlib import Path

from vq_census import census


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root
    rows, checks = [], {}
    for run, artifact, repeat, selected in (
        ("run1-quant0", "q0", 2, ""),
        ("run2-quant1", "q1", 2, ""),
        ("run3-dsl0", "d0", 3, "reduce"),
        ("run4-dsl1", "d1", 3, "reduce"),
        ("run5-banks", "banks", 2, ""),
        ("run6-validation", "validation", 1, ""),
        ("run8-offsets", "direct-witness", 1, ""),
    ):
        result = json.loads((root / f"{run}.json").read_text(encoding="utf-8"))
        cases = pickle.loads((root / f"{artifact}.pkl").read_bytes())["cases"]
        total = Counter()
        for case in cases:
            if selected and selected not in case["label"]:
                continue
            work = census(case["compiled"]["l1"])
            total.update({k: v * repeat for k, v in work.items()})
            record = next(r for r in result["records"] if r["label"] == case["label"])
            rows.append(
                {
                    "session": run,
                    "label": case["label"],
                    "cycles": record["cycles"],
                    "work": work,
                }
            )
        if run == "run8-offsets":
            negative = pickle.loads((root / "negative-fixed.pkl").read_bytes())[
                "cases"
            ][0]
            total.update(census(negative["compiled"]["l1"]))
            assert result["records"][-1]["label"] == "pipeline-live-infinity"
        counters = next(iter(result["counters"].values()))["VRES"]
        checks[run] = {
            k: {
                "census": total[k],
                "model": counters["lanes" if k == "lane_slots" else k],
            }
            for k in ("lane_slots", "fill_words", "drain_words")
        }
        assert all(c["census"] == c["model"] for c in checks[run].values()), checks[run]
    (root / "roofline.json").write_text(
        json.dumps({"rows": rows, "checks": checks}, indent=2), encoding="utf-8"
    )
    lines = [
        "# Loop7 per-kernel executed-work rooflines",
        "",
        "Model package cycles, not owner 200/400 silicon cycles. Floor=max(fill words, drain words, lane slots/16); excludes memory latency, fetch/launch and mover engines. Repetitions and session boundaries are explicit. Session6 checked 40 kernels before a harness label lookup failed; session8 supplies the missing live-infinity witness. Session7 opened the model but failed machine identity before kernels, so has no roofline row.",
        "",
        "| Session | Kernel | Cycles | VC bytes | Mover bytes | Lane slots | Floor cycles | Floor / last cycles |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        w = row["work"]
        lines.append(
            f"| {row['session']} | {row['label']} | {', '.join(str(c) for c in row['cycles'])} | {w['vector_bytes']:,} | {w.get('mover_read_bytes',0)+w.get('mover_write_bytes',0):,} | {w.get('lane_slots',0):,} | {w['engine_floor_cycles']:,} | {100*w['engine_floor_cycles']/row['cycles'][-1]:.2f}% |"
        )
    (root / "roofline.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(rows), "checks": checks}, indent=2))


if __name__ == "__main__":
    main()
