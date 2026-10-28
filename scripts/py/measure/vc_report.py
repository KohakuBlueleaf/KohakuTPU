"""Render R63's raw measurements and per-case verifier inventory into its report."""

import argparse
import collections
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def witnesses(value):
    if isinstance(value, dict):
        if "verdict" in value:
            yield value
        else:
            for child in value.values():
                yield from witnesses(child)
    elif isinstance(value, list):
        for child in value:
            yield from witnesses(child)


def render(work):
    """`work` holds `consequences-*.json` (vc_consequences.py), `promoted-*.json`
    (the r63_promote example) and the `report-body.md` template."""
    raw = []
    count = 0
    for path in sorted((ROOT / "machines/measured").glob("vc_timing.*.json")):
        record = json.loads(path.read_text())
        assert record["parameters"]["seti"]["value"] == 3
        assert record["parameters"]["mx7_issue_gap"]["value"] == 1
        raw += [
            f"### [{path.name}](machines/measured/{path.name})",
            "",
            "| Bench | VL | Form | Elapsed at K | b | a | max residual |",
            "|---|---:|:---:|---|---:|---:|---:|",
        ]
        for p in record["probes"]:
            f = p["fit"]
            count += len(p["runs"])
            assert all(not r["faults"] and not r["warn"] for r in p["runs"])
            elapsed = ",".join(str(r["elapsed"]) for r in p["runs"])
            dep = "D" if p["dependent"] else "I"
            residual = max(map(abs, f["residuals"]))
            raw.append(
                f"| {p['kind']} | {p['vl']} | {dep} | {elapsed} | "
                f"{f['slope']:.6f} | {f['intercept']:.6f} | {residual:.6f} |"
            )
        raw.append("")
    assert count == 1188
    audit = [
        row
        for path in sorted(work.glob("consequences-*.json"))
        for row in json.loads(path.read_text())
    ]
    assert len(audit) == 53
    for row in audit:
        assert row["same_l2"]
        assert row["old"]["verdict"] == row["new"]["verdict"]
        assert (
            not row["new"]["verdict"]["refused"]
            or row["case"] == "examples-dsl-attention"
        )
    cases = collections.defaultdict(list)
    for path in sorted(work.glob("promoted-*.json")):
        for row in json.loads(path.read_text()):
            cases[row["family"], row["case"]].append(row)
    inventory = ["| Case | P/S | Verdict | Changed fields |", "|---|:---:|:---:|---|"]
    for (family, name), rows in sorted(cases.items()):
        found = [w for row in rows for w in witnesses(row["evidence"])]
        assert found, (family, name)
        fields = sorted({f for row in rows for f in row["fields"]})
        if family == "e2e":
            fields.append("timing_measured (new provenance)")
        refused = any(w["verdict"]["refused"] for w in found)
        classes = {w["classification"] for w in found}
        classes.update(row["evidence"].get("classification", "P") for row in rows)
        cls = "S" if "S" in classes else "P"
        status = "A"
        if refused:
            if "examples-dsl-attention" in name:
                status = "R"
            else:
                assert name == "vec-l1-sched-L1.SCHED.002.l1-words"
                assert all(w["old_l1"] == w["new_l1"] for w in found)
                status = "N"
        inventory.append(
            f"| {family}/{name} | {cls} | {status} | {', '.join(fields)} |"
        )
    totals = collections.Counter(f for f, _ in cases)
    inventory += ["", f"Inventory totals: {dict(sorted(totals.items()))}."]
    body = (work / "report-body.md").read_text(encoding="utf-8")
    return body.replace("{{RAW_TABLES}}", "\n".join(raw)).replace(
        "{{INVENTORY}}", "\n".join(inventory)
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--work",
        type=Path,
        required=True,
        help="directory of the consequence and promotion records and report-body.md",
    )
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    text = render(args.work)
    output = args.output
    output.write_text(text, encoding="utf-8")
    assert output.read_text(encoding="utf-8") == text
    print(output, len(text.splitlines()), "lines; raw/audit assertions passed")


if __name__ == "__main__":
    main()
