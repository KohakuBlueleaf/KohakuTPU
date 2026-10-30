"""Account for unchanged lanes launches with exact singleton template proofs."""

import argparse
import json
import pathlib
import time

from derive import compile_graph
from kohakutpu.stack import rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    args = parser.parse_args()
    doc = json.loads(args.capture.read_text(encoding="utf-8"))
    machine = doc["machine_json"]
    groups = {}
    for launch, entry in enumerate(doc["launches"]):
        record = doc["records"][entry["record"]]
        if record["generator"] == "lanes":
            source = json.dumps(dict(record["graph"], name="template"), sort_keys=True)
            groups.setdefault(source, []).append(launch)
    started = time.perf_counter()
    artifacts = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for source, launches in groups.items():
        graph = json.loads(source)
        first, _ = compile_graph(graph, machine)
        direct, _ = compile_graph(graph, machine)
        proofs = {}
        for mesh, package in first["packages"].items():
            template = rs._rs.template_derive([package["bytes"]], "[{}]", "{}", machine)
            bindings = [
                0x10000000 + i * 0x1000000 for i in range(len(package["params"]))
            ]
            proofs[str(mesh)] = rs._rs.template_prove(
                template["bytes"],
                template["template_json"],
                direct["packages"][mesh]["bytes"],
                "{}",
                bindings,
                machine,
                f"unchanged lanes {launches}",
            )
        artifacts.append({"launches": launches, "proofs": proofs})
        print(
            f"lanes singleton {len(artifacts)}/{len(groups)}: {len(launches)} launches proven",
            flush=True,
        )
    result = {
        "status": "unchanged_exact_singletons",
        "launches": sum(len(a["launches"]) for a in artifacts),
        "templates": len(artifacts),
        "wall_seconds": time.perf_counter() - started,
        "artifacts": artifacts,
    }
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "artifacts"}))


if __name__ == "__main__":
    main()
