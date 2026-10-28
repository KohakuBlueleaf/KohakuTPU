"""Measure class policies on every captured non-lanes launch, with cold compilation."""

import argparse
import json
import sys
import time
from pathlib import Path

from capture import GENERATORS
from derive import compile_graph, decode
from overhead import sizes

from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    doc = json.loads(args.capture.read_text(encoding="utf-8"))
    machine = OfflineCard(doc["board"]).machine
    machine_json = rs.machine_json(machine)
    kblock = json.loads(machine_json)["machine"]["mg"]["kblock"]
    factories = {kind: getattr(module, name) for module, name, kind in GENERATORS}
    report = {
        "command": "python " + " ".join(sys.argv),
        "board": doc["board"],
        "measurements": [],
    }
    for policy in ("exact", "pow2", "coarse"):
        for cores in ("all", "min"):
            started = time.perf_counter()
            cache, classes, rows = {}, set(), []
            for launch, entry in enumerate(doc["launches"]):
                record = doc["records"][entry["record"]]
                family = record["generator"]
                if family not in (
                    "rows",
                    "flat",
                    "mm",
                    "quant",
                    "walk",
                    "transpose",
                    "copy",
                ):
                    continue
                row = {"launch": launch, "family": family}
                try:
                    if entry["record"] not in cache:
                        kernel, given, *_ = factories[family](
                            **decode(record["arguments"]),
                            shape_class=ShapeClass(
                                kblock, 256, policy=policy, cores=cores
                            ),
                        )
                        graph = kernel.trace(
                            given, machine=K.dsl_machine(machine)
                        ).graph
                        compiled, seconds = compile_graph(graph, machine_json)
                        info = kernel.class_info
                        cache[entry["record"]] = {
                            "sizes": sizes(compiled, machine_json),
                            "class_info": info,
                            "compile_seconds": seconds,
                        }
                    row.update(cache[entry["record"]])
                    classes.add(
                        json.dumps(
                            row["class_info"].get("key", row["class_info"]),
                            sort_keys=True,
                        )
                    )
                except (ValueError, RuntimeError) as error:
                    row["error"] = str(error)
                rows.append(row)
            measurement = {
                "policy": policy,
                "cores": cores,
                "seconds": time.perf_counter() - started,
                "cold_compile_seconds": sum(
                    row["compile_seconds"] for row in cache.values()
                ),
                "distinct_records_compiled": len(cache),
                "class_metadata_keys": len(classes),
                "launches": rows,
                "failures": sum("error" in row for row in rows),
            }
            report["measurements"].append(measurement)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print({k: v for k, v in measurement.items() if k != "launches"}, flush=True)


if __name__ == "__main__":
    main()
