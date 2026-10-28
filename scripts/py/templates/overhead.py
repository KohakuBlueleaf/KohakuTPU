"""Compare direct offline class packages with the saved unclassed capture."""

import argparse
import json
import pathlib
import time
from collections import Counter

from capture import GENERATORS
from derive import compile_graph, decode

from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.compiler import _rs
from kohakutpu.stack import kernel as K


def sizes(result, machine_json):
    packages = [
        _rs.package_read(p["bytes"], machine_json, mesh)
        for mesh, p in result["packages"].items()
    ]
    ops = _rs.package_format(machine_json)["op"]
    return {
        "steps": sum(len(p["steps"]) for p in packages),
        "payloads": sum(len(p["payloads"]) for p in packages),
        "bytes": sum(len(p["bytes"]) for p in result["packages"].values()),
        "buffer_bytes": sum(b["size"] for p in packages for b in p["buffers"]),
        "register_control": any(
            s[0] in (ops["JUMP"], ops["PATCH"]) for p in packages for s in p["steps"]
        ),
    }


def measure(capture, output, cores, family="rows"):
    doc = json.loads(capture.read_text(encoding="utf-8"))
    machine = OfflineCard(doc["board"]).machine
    policy = ShapeClass(
        json.loads(doc["machine_json"])["machine"]["mg"]["kblock"], 256, cores=cores
    )
    rows, totals = [], Counter()
    started = time.perf_counter()
    for launch, entry in enumerate(doc["launches"]):
        record = doc["records"][entry["record"]]
        if record["generator"] != family:
            continue
        row = {"launch": launch}
        try:
            baseline, _ = compile_graph(record["graph"], doc["machine_json"])
            factory = next(
                getattr(module, name)
                for module, name, kind in GENERATORS
                if kind == family
            )
            kernel, given, *_ = factory(
                **decode(record["arguments"]), shape_class=policy
            )
            graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
            classed, _ = compile_graph(graph, doc["machine_json"])
            row.update(
                baseline=sizes(baseline, doc["machine_json"]),
                classed=sizes(classed, doc["machine_json"]),
                class_info=kernel.class_info,
            )
            for key in ("steps", "payloads", "bytes", "buffer_bytes"):
                row[f"extra_{key}"] = row["classed"][key] - row["baseline"][key]
                totals[f"extra_{key}"] += row[f"extra_{key}"]
            totals["padded_rows"] += kernel.class_info["padded_rows"]
            totals["padded_columns"] += kernel.class_info["padded_columns"]
            totals["padded_elements"] += kernel.class_info.get("padded_elements", 0)
        except (ValueError, RuntimeError) as error:
            row["error"] = str(error)
            totals["failed"] += 1
        rows.append(row)
    result = {
        "core_policy": cores,
        "launches": len(rows),
        "wall_seconds": time.perf_counter() - started,
        "totals": dict(totals),
        "launch_costs": rows,
        "units": "static package steps/words/bytes; padding rows are class staging rows, including block-major rows",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "launch_costs"}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    parser.add_argument("--cores", choices=["all", "min"], default="all")
    parser.add_argument(
        "--family",
        choices=["rows", "flat", "mm", "quant", "walk", "transpose", "copy"],
        default="rows",
    )
    args = parser.parse_args()
    measure(args.capture, args.output, args.cores, args.family)


if __name__ == "__main__":
    main()
