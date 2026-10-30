"""Join isolated measurements and counter-checked L1 work into per-kernel tables."""

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
    directory = args.directory
    final = pickle.loads((directory / "final.pkl").read_bytes())
    final_cases = {(c["model"], c["key"]): c for c in final["cases"]}
    last = json.loads((directory / "run8-final.json").read_text())
    final_runs = {(r["model"], r["key"]): r for r in last["records"] if "model" in r}
    summaries = {}
    for model, run in (("lenet", "run6-lenet"), ("vit", "run7-vit")):
        bundle = pickle.loads((directory / f"{model}-variants.pkl").read_bytes())
        measured = json.loads((directory / f"{run}.json").read_text())
        records = {r["key"]: r for r in measured["records"]}
        rows, totals, by_kind = [], Counter(), {}
        text = [
            f"# {model}: isolated full-shape kernel roofline",
            "",
            "Cycles are firmware-reported package cycles, not vector-only busy. Input tensors come from offline capture; this is not an end-to-end numerical workload. Each unchanged package was measured once, not rerun with a no-op knob. Repeated exact classes carry their capture multiplicity. VC bytes and math lane slots are executed counts from L1, independently checked against aggregate VRES. Mover payload/index bytes are separate static counts; matmul-engine bytes/ops and instruction traffic are excluded from the VC floor.",
            "",
            "`floor=max(VC read bytes/32, VC write bytes/32, math lane slots/16)`. Efficiency is this engine floor / package cycles. It is an optimistic lower bound, not a pack/NoC timing model. Lane slots count the implementation's issued work, so compare each variant against its own floor.",
            "",
            "| Kernel (key prefix) | Kind | Multiplicity | Cycles before | Pipeline only | Final cycles | VC bytes | Lane slots before → after | Floor before → after | Efficiency before → after |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for case in bundle["cases"]:
            key = (model, case["key"])
            measured_case = records[case["key"]]
            before = census(case["compiled"]["l1"])
            after = (
                census(final_cases[key]["final"]["l1"])
                if key in final_cases
                else before
            )
            cycles = (
                final_runs[key]["after"]
                if key in final_runs
                else measured_case["before"]
            )
            b = measured_case["before"]
            row = dict(
                measured_case,
                final=cycles,
                before_work=before,
                final_work=after,
                before_efficiency=before["engine_floor_cycles"] / b,
                final_efficiency=after["engine_floor_cycles"] / cycles,
                counter_scope="session aggregate, not per-kernel stalls",
            )
            rows.append(row)
            count = row["multiplicity"]
            by_kind.setdefault(row["kind"], Counter())
            for label, value in (
                ("before", b),
                ("pipeline", row["after"]),
                ("final", cycles),
            ):
                totals[label] += value * count
                by_kind[row["kind"]][label] += value * count
            text.append(
                f"| {row['name']} ({row['key'][:8]}) | {row['kind']} | {count} | {b:,} | {row['after']:,} | {cycles:,} | {after['vector_bytes']:,} | {before['lane_slots']:,} → {after['lane_slots']:,} | {before['engine_floor_cycles']:,} → {after['engine_floor_cycles']:,} | {row['before_efficiency']:.2%} → {row['final_efficiency']:.2%} |"
            )
        checks = json.loads((directory / f"{model}-roofline.json").read_text())[
            "counter_checks"
        ]
        doc = {
            "model": model,
            "rows": rows,
            "totals": totals,
            "by_kind": by_kind,
            "counter_checks": checks,
            "kind": "multiplicity-weighted isolated estimate, not measured whole-step",
        }
        (directory / f"{model}-final-roofline.json").write_text(
            json.dumps(doc, indent=2), encoding="utf-8"
        )
        (directory / f"{model}-roofline.md").write_text(
            "\n".join(text) + "\n", encoding="utf-8"
        )
        summaries[model] = {
            "classes": len(rows),
            "launches": sum(r["multiplicity"] for r in rows),
            "totals": totals,
            "by_kind": by_kind,
        }
    (directory / "summary.json").write_text(
        json.dumps(summaries, indent=2), encoding="utf-8"
    )
    print(json.dumps(summaries, indent=2))


if __name__ == "__main__":
    main()
