"""Join stream experiments with the prior loop and validate executed counters."""

import argparse
import json
import pickle
from collections import Counter
from pathlib import Path

from vq_census import census


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    prior = root.parent / "loop2"
    checks = {}
    for name, artifact, repeats in (
        ("run5-pull-t0", "pull-t0", 2),
        ("run6-pull-t1", "pull-t1", 2),
        ("run7-pull-witness", "pull-witness", 2),
        ("run8-quant-witness", "quant-witness", 1),
    ):
        measured = json.loads((root / f"{name}.json").read_text(encoding="utf-8"))
        bundle = pickle.loads((root / f"{artifact}.pkl").read_bytes())
        work = Counter()
        for case in bundle["cases"]:
            work.update(
                {k: v * repeats for k, v in census(case["compiled"]["l1"]).items()}
            )
        if repeats == 1:
            negative = pickle.loads((root / "negative.pkl").read_bytes())["cases"][0]
            work.update(census(negative["compiled"]["l1"]))
        actual = next(iter(measured["counters"].values()))["VRES"]
        checks[name] = {
            k: dict(census=work[k], model=actual["lanes" if k == "lane_slots" else k])
            for k in ("fill_words", "drain_words", "lane_slots")
        }
        assert all(c["census"] == c["model"] for c in checks[name].values())
    measured = json.loads(
        (root / "run8-quant-witness.json").read_text(encoding="utf-8")
    )
    new = {r["label"]: r for r in measured["records"] if "cycles" in r}
    cases = {
        c["label"]: c
        for c in pickle.loads((root / "quant-witness.pkl").read_bytes())["cases"]
    }
    old_cases = {
        c["label"]: c for c in pickle.loads((prior / "final.pkl").read_bytes())["cases"]
    }
    previous = json.loads((prior / "run8-final.json").read_text(encoding="utf-8"))
    old = {r["label"]: r for r in previous["records"] if "model" in r}
    rows, totals = [], {}
    for label, r in new.items():
        before = census(old_cases[label]["final"]["l1"])
        after = census(cases[label]["compiled"]["l1"])
        row = dict(
            label=label,
            model=old[label]["model"],
            before=old[label]["after"],
            after=r["cycles"][0],
            before_work=before,
            after_work=after,
            reduction=1 - r["cycles"][0] / old[label]["after"],
        )
        rows.append(row)
    for model in ("lenet", "vit"):
        inventory = json.loads(
            (prior / f"{model}-final-roofline.json").read_text(encoding="utf-8")
        )
        multiplicities = {r["name"]: r["multiplicity"] for r in inventory["rows"]}
        totals[model] = {
            variant: sum(
                r[variant] * multiplicities[r["label"]]
                for r in rows
                if r["model"] == model
            )
            for variant in ("before", "after")
        }
    text = [
        "# Quant stream per-kernel comparison",
        "",
        "Prior-loop baseline uses offline capture inputs; this loop uses patterned nonzero FP16 and an independent MX7 reference. Cycles from different sessions are not controlled timing deltas. Use the four isolated runs for causal knob comparison. VC floor is max(read words, write words, lane slots/16); instruction and matmul traffic are excluded.",
        "",
        "| Kernel | Model | Previous cycles | Stream cycles | VC bytes | Lane slots before → after | Images before → after | Fill requests before → after | Floor before → after |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        b, a = r["before_work"], r["after_work"]
        text.append(
            f"| {r['label']} | {r['model']} | {r['before']:,} | {r['after']:,} | {a['vector_bytes']:,} | {b['lane_slots']:,} → {a['lane_slots']:,} | {b['image_runs']} → {a['image_runs']} | {b.get('fill_requests',0)} → {a.get('fill_requests',0)} | {b['engine_floor_cycles']:,} → {a['engine_floor_cycles']:,} |"
        )
    (root / "quant-roofline.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    (root / "summary.json").write_text(
        json.dumps(dict(rows=rows, totals=totals, checks=checks), indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            dict(
                totals=totals,
                regressions=[
                    (r["label"], r["reduction"]) for r in rows if r["reduction"] < 0
                ],
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
