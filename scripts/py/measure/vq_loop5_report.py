"""Reconcile Loop5 model counters and publish executed per-kernel inventory tables."""

import argparse
import json
import pickle
from collections import Counter
from pathlib import Path

from vq_census import census


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def bundle(path):
    return pickle.loads(path.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    checks = {}
    for run, artifact, repeats, selected in (
        ("run1-zero0", "zero0", 2, ""),
        ("run2-zero1", "zero1", 2, ""),
        ("run3-band1-reduce", "band1", 3, "reduce"),
        ("run4-band4-reduce", "band4", 3, "reduce"),
        ("run5-validation", "validation", 1, ""),
        ("run6-lenet", "lenet", 1, ""),
        ("run7-vit", "vit", 1, ""),
        ("run8-tail", "tail", 2, ""),
    ):
        measured = load(root / f"{run}.json")
        cases = bundle(root / f"{artifact}.pkl")["cases"]
        work = Counter()
        for case in cases:
            if selected and selected not in case["label"]:
                continue
            work.update(
                {k: v * repeats for k, v in census(case["compiled"]["l1"]).items()}
            )
        if run == "run5-validation":
            negative = bundle(root / "negative.pkl")["cases"][0]
            work.update(census(negative["compiled"]["l1"]))
        counters = next(iter(measured["counters"].values()))["VRES"]
        checks[run] = {
            key: dict(
                census=work[key],
                model=counters["lanes" if key == "lane_slots" else key],
            )
            for key in ("fill_words", "drain_words", "lane_slots")
        }
        assert all(c["census"] == c["model"] for c in checks[run].values()), checks[run]
    all_rows, totals = {}, {}
    for model, session in (("lenet", 6), ("vit", 7)):
        current = bundle(root / f"{model}.pkl")["cases"]
        old = bundle(root.parent / f"loop2/{model}-variants.pkl")["cases"]
        measured = load(root / f"run{session}-{model}.json")["records"]
        old_measured = load(root.parent / f"loop2/run{session}-{model}.json")["records"]
        assert len(current) == len(old) == len(measured) == len(old_measured)
        rows = []
        counts = {"before": Counter(), "after": Counter()}
        for index, (a, b, before, after) in enumerate(
            zip(old, current, old_measured, measured)
        ):
            assert a["kind"] == b["kind"]
            assert a["multiplicity"] == b["multiplicity"]
            assert before["key"] == a["key"] and after["key"] == b["key"]
            # Pull tree changes only internal selector-mask port extents.
            pa = [p for p in a["graph"]["ports"] if "mask" not in p["name"]]
            pb = [p for p in b["graph"]["ports"] if "mask" not in p["name"]]
            assert pa == pb, (model, index)
            wa, wb = census(a["compiled"]["l1"]), census(b["compiled"]["l1"])
            row = dict(
                index=index,
                name=b["label"],
                previous_name=a["label"],
                kind=b["kind"],
                multiplicity=b["multiplicity"],
                before=before["before"],
                after=after["after"],
                work_before=wa,
                work_after=wb,
                hashes_equal=before["output_sha256"] == after["output_sha256"],
            )
            rows.append(row)
            for mode in ("before", "after"):
                counts[mode][b["kind"]] += row[mode] * b["multiplicity"]
        all_rows[model], totals[model] = rows, counts
        lines = [
            f"# {model} full isolated inventory — Loop2 baseline and Loop5 current defaults",
            "",
            "Separate-process isolated workload estimates, not end-to-end cycles. Capture inputs include offline placeholder values: output digests witness replay stability, not independent graph correctness. Independent nonzero quant/class witnesses are separate. Class-template mode is not activated by these exact-graph captures. VC floor=max(fill words, drain words, lane slots/16); package/mover/fetch costs remain outside that floor. Counter attribution from these inventory sessions is aggregate, not per kernel.",
            "",
            "| # | Kernel | Kind | Multiplicity | Cycles old → new | VC bytes old → new | Lane slots old → new | Floor old → new | New floor/cycles | Mover bytes new |",
            "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for row in rows:
            a, b = row["work_before"], row["work_after"]
            lines.append(
                f"| {row['index']} | {row['name']} | {row['kind']} | {row['multiplicity']} | {row['before']:,} → {row['after']:,} | {a.get('vector_bytes',0):,} → {b.get('vector_bytes',0):,} | {a.get('lane_slots',0):,} → {b.get('lane_slots',0):,} | {a['engine_floor_cycles']:,} → {b['engine_floor_cycles']:,} | {100*b['engine_floor_cycles']/row['after']:.2f}% | {b.get('mover_read_bytes',0)+b.get('mover_write_bytes',0):,} |"
            )
        (root / f"{model}-roofline.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
    result = dict(checks=checks, totals=totals, rows=all_rows)
    (root / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            dict(
                checks=checks,
                totals=totals,
                changed_hashes={
                    m: [r["index"] for r in rows if not r["hashes_equal"]]
                    for m, rows in all_rows.items()
                },
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
