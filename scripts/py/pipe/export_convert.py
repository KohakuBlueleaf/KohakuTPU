"""Native predicate-select A/B for captured convert kernels."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def serial(body):
    result = []
    for original in body:
        st = dict(original)
        if st["kind"] == "par" and st["unit"]["kind"] in ("vc", "mg"):
            for i in range(st["lo"], st["hi"]):
                part = dict(
                    st,
                    lo=i,
                    hi=i + 1,
                    unit=dict(st["unit"], index=0),
                    body=serial(st["body"]),
                )
                result.append(part)
        else:
            if "body" in st:
                st["body"] = serial(st["body"])
            result.append(st)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path)
    ap.add_argument("--key", default="")
    ap.add_argument("--kind", default="convert")
    ap.add_argument(
        "--manifest", type=Path, required=True, help="captured kernel manifest JSON"
    )
    args = ap.parse_args()
    rows = json.loads(args.manifest.read_text())
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    args.output.mkdir(parents=True, exist_ok=True)
    for row in rows:
        if row["kind"] != args.kind or not row["key"].startswith(args.key):
            continue
        if row["source"] is None:
            print(row["key"][:10], "missing exact source", flush=True)
            continue
        graph = json.loads(row["source"]["source"])
        graph["machine"] = {"mg": 1, "vc": 1, "nodes": 1}
        graph["body"] = serial(graph["body"])
        for label, mutant in (("before", "l2.predicate-select/off"), ("after", None)):
            with rs.mutant(mutant):
                c = K.compile(json.dumps(graph), name=graph["name"], machine=machine)
            rec = {
                "key": row["key"],
                "label": label,
                "name": c.name,
                "l1": c.l1_text,
                "verdict": c.verdict,
                "ports": graph["ports"],
            }
            (args.output / f"{row['key']}-{label}.json").write_text(json.dumps(rec))
            print(row["key"][:10], label, c.verdict["refused"], flush=True)


if __name__ == "__main__":
    main()
