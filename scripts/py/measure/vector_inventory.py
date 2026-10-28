"""Inventory prebuild captures without mistaking port extents for traffic.

Each exact class gets a row with its signature, launch multiplicity and quant
contract. Executed bytes, lane operations and cycles require dynamic evidence.
"""

import argparse
import json
import math
from collections import Counter
from pathlib import Path

ITEM_BYTES = {"f16": 2, "f32": 4, "mx7a": 1, "mx7b": 1}


def statements(body):
    for node in body:
        yield node
        yield from statements(node.get("body", ()))


def inventory(doc):
    launches = Counter(doc["launches"])
    rows = []
    for key, entry in doc["sources"].items():
        graph = json.loads(entry["source"])
        nodes = list(statements(graph["body"]))
        quant = [node for node in nodes if node.get("op") == "quant"]
        finite = sum(dict(node["kw"]).get("finite") == {"int": 1} for node in quant)
        overlap = sum(
            dict(node["kw"]).get("classifier_overlap") == {"int": 1} for node in quant
        )
        ports = [
            dict(
                name=p["name"],
                role=p["role"],
                shape=p["spec"]["shape"],
                dtype=p["spec"]["dtype"],
                extent_bytes=math.prod(p["spec"]["shape"])
                * ITEM_BYTES[p["spec"]["dtype"]],
            )
            for p in graph["ports"]
        ]
        rows.append(
            dict(
                key=key,
                name=graph["name"],
                family=entry["family"],
                capture_launches=launches[key],
                ports=ports,
                quant_statements=len(quant),
                finite_quant_statements=finite,
                classifier_overlap_statements=overlap,
                before_cycles=None,
                after_cycles=None,
                bytes_moved=None,
                math_lane_ops=None,
                roofline_cycles=None,
                achieved_fraction=None,
                stall_cause="unmeasured: prescribed model unavailable",
            )
        )
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    doc = json.loads(args.capture.read_text(encoding="utf-8"))
    rows = inventory(doc)
    summary = dict(
        model=doc["model"],
        batch=doc["batch"],
        exact_classes=len(rows),
        capture_launches=len(doc["launches"]),
        phase_launches=doc["phase_launches"],
        last_phase_launches=doc["last_phase_launches"],
        quant_classes=sum(r["quant_statements"] > 0 for r in rows),
        finite_quant_classes=sum(r["finite_quant_statements"] > 0 for r in rows),
        classifier_overlap_classes=sum(
            r["classifier_overlap_statements"] > 0 for r in rows
        ),
        families=dict(Counter(r["name"].split("_")[0] for r in rows)),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(dict(summary=summary, kernels=rows), indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
