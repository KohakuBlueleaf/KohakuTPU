"""A traced run's step records, on the host: cycles between steps by opcode, per
launch, and each launch's passes: consecutive steps of one opcode as one row, in package
order, so a transpose or a gather reads pass by pass. Records come back with whichever
collect reads them, so a package's steps are joined by tag and tags follow the run order.

    python measurements/tools/steps.py TRACE_DIR OUT.json
"""

import collections
import json
import pathlib
import re
import sys


def main():
    trace, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    rows = [
        json.loads(x) for x in (trace / "card.jsonl").read_text("utf-8").splitlines()
    ]
    by_tag: dict = collections.defaultdict(list)
    for r in rows:
        for p in (r.pop("trace", None) or {}).get("packages") or []:
            by_tag[p["tag"]].extend(p.get("steps") or [])
    tags = sorted(by_tag)
    gathers = {
        path.stem
        for path in (trace / "kernels").glob("*.l2")
        if re.search(r"^\s*gather\b", path.read_text("utf-8"), re.MULTILINE)
    }
    result = []
    for r, tag in zip(rows, tags, strict=False):
        ops: dict = collections.Counter()
        count: dict = collections.Counter()
        passes: list = []
        prev = 0
        for s in by_tag[tag]:
            ops[s["op"]] += s["cycle"] - prev
            count[s["op"]] += 1
            if passes and passes[-1]["op"] == s["op"]:
                passes[-1]["steps"] += 1
                passes[-1]["cycles"] += s["cycle"] - prev
            else:
                passes.append({"op": s["op"], "steps": 1, "cycles": s["cycle"] - prev})
            prev = s["cycle"]
        result.append(
            {
                "name": r["name"],
                "kind": r["kind"],
                "gather": r["name"] in gathers,
                "cycles": r["cycles"],
                "steps": r["steps"],
                "tail": r["steps"] - prev,
                "ops": ops,
                "counts": count,
                "passes": passes,
            }
        )
    out.write_text(json.dumps(result), "utf-8")
    print(len(rows), "rows", len(tags), "tags")
    for r in sorted(result, key=lambda x: -x["cycles"])[:8]:
        print(
            r["name"][:12],
            r["kind"],
            r["cycles"],
            "tail",
            r["tail"],
            dict(r["ops"]),
            dict(r["counts"]),
        )


if __name__ == "__main__":
    main()
