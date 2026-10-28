"""Measure host/register step-count binding on a saved differential proof ledger."""

import argparse
import json
import pathlib
from collections import Counter

from derive import parameters

from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs


def measure(capture, results, output):
    doc = json.loads(capture.read_text(encoding="utf-8"))
    if "board" in doc:
        doc["machine_json"] = rs.machine_json(OfflineCard(doc["board"]).machine)
    ledger = json.loads(results.read_text(encoding="utf-8"))
    root = results.parent / "experimental-templates"
    rows = []
    totals = Counter()
    seen = set()
    for artifact in ledger["artifacts"]:
        for launch in artifact["launches"]:
            if launch in seen:
                raise ValueError(f"duplicate launch {launch}")
            seen.add(launch)
            record = doc["records"][doc["launches"][launch]["record"]]
            params = parameters(
                record["arguments"],
                aligned_rows="W:columns32" in artifact.get("bounds", {}),
                class_walk="walk/matched" in artifact.get("bounds", {}),
                class_transpose="A:units16" in artifact.get("bounds", {}),
                quant_kblock=(
                    json.loads(doc["machine_json"])["machine"]["mg"]["kblock"]
                    if any(
                        key.startswith("quant/") for key in artifact.get("bounds", {})
                    )
                    else None
                ),
                mm_kblock=(
                    json.loads(doc["machine_json"])["machine"]["mg"]["kblock"]
                    if any(":units" in key for key in artifact.get("bounds", {}))
                    else None
                ),
            )
            params = json.loads(
                _rs.template_values(
                    json.dumps(artifact.get("values", {})), json.dumps(params)
                )
            )
            for mesh, stem in artifact["meshes"].items():
                raw = (root / f"{stem}.bin").read_bytes()
                meta = (root / f"{stem}.json").read_text(encoding="utf-8")
                table = json.loads(meta)
                count = sum(s.get("step") is not None for s in table["sites"])
                package = _rs.package_read(raw, doc["machine_json"], int(mesh))
                bindings = [
                    0x10000000 + i * 0x100000 for i in range(len(package["buffers"]))
                ]
                row = {
                    "launch": launch,
                    "generator": record["generator"],
                    "mesh": mesh,
                    "step_sites": count,
                }
                for path in ("step", "register"):
                    try:
                        bound = _rs.bind(
                            raw,
                            bindings,
                            doc["machine_json"],
                            mesh=int(mesh),
                            params=params,
                            template_json=meta,
                            count_path=path,
                        )
                    except ValueError as error:
                        if path == "step":
                            raise
                        row[path] = {"refused": str(error)}
                        totals["register_refused"] += 1
                        continue
                    package = _rs.package_read(bound, doc["machine_json"], int(mesh))
                    row[path] = {
                        "steps": len(package["steps"]),
                        "payloads": len(package["payloads"]),
                        "bytes": len(bound),
                    }
                if "refused" in row["register"]:
                    totals["step_sites"] += count
                    rows.append(row)
                    continue
                for key in ("steps", "payloads", "bytes"):
                    difference = row["register"][key] - row["step"][key]
                    row[f"extra_{key}"] = difference
                    totals[f"extra_{key}"] += difference
                if (
                    row["extra_steps"] != 2 * count
                    or row["extra_bytes"] != 32 * count
                    or row["extra_payloads"]
                ):
                    raise AssertionError(f"count-path cost differs: {row}")
                totals["step_sites"] += count
                rows.append(row)
    expected = sum(r["proven"] for r in ledger["generators"].values())
    if len(seen) != expected:
        raise ValueError("measurement does not cover the proof ledger")
    result = {
        "launches": len(seen),
        "packages": len(rows),
        "totals": dict(totals),
        "launch_costs": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "launch_costs"}, sort_keys=True
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=pathlib.Path)
    parser.add_argument("results", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    args = parser.parse_args()
    measure(args.capture, args.results, args.output)


if __name__ == "__main__":
    main()
