"""Audit timing-only machine changes without rewriting golden expectations."""

import argparse
import collections
import difflib
import json
from pathlib import Path

from kohakutpu.compiler import _rs

ROOT = Path(__file__).resolve().parents[3]


def compile_case(case, machine):
    try:
        out = _rs.compile(case["l2"], machine, name=case.get("name"), cache_dir="")
        return {"l1": out["l1"], "l2": out["l2"], "verdict": out["verdict"]}
    except _rs.Refused as e:
        return {"refused": {"stage": e.stage, "clause": e.clause, "reason": str(e)}}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shard", type=int, required=True)
    ap.add_argument(
        "--out-dir",
        type=Path,
        required=True,
        help="directory receiving consequences-<shard>.json",
    )
    args = ap.parse_args()
    records = []
    paths = sorted((ROOT / "software/rust/golden/e2e").glob("*.json"))
    for index, path in enumerate(paths):
        if index % 16 != args.shard:
            continue
        case = json.loads(path.read_text())
        if not case.get("l2"):
            continue
        machine = json.loads(
            (ROOT / "machines" / f"{case['machine']}.json").read_text()
        )
        new = compile_case(case, json.dumps(machine))
        machine["machine"]["vc"]["timing"].update(
            xbar=7, unpack_math=12, seti=2, mx7_issue_gap=0
        )
        old = compile_case(case, json.dumps(machine))
        if old == new:
            continue
        record = {"case": case["case"], "old": old, "new": new}
        if "l1" in old and "l1" in new:
            record["same_l2"] = old["l2"] == new["l2"]
            record["line_multiset_same"] = collections.Counter(
                old["l1"].splitlines()
            ) == collections.Counter(new["l1"].splitlines())
            record["diff"] = list(
                difflib.unified_diff(
                    old["l1"].splitlines(),
                    new["l1"].splitlines(),
                    fromfile="old timing",
                    tofile="measured timing",
                    lineterm="",
                )
            )
        records.append(record)
        accepted = "verdict" in new and not new["verdict"]["refused"]
        print(
            case["case"],
            "check accepted" if accepted else new.get("verdict", new),
            flush=True,
        )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"consequences-{args.shard:02}.json"
    out.write_text(json.dumps(records, indent=2) + "\n")
    print(out, len(records))


if __name__ == "__main__":
    main()
