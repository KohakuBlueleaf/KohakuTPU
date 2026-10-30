"""Classify rebuilt production templates and time sampling, box proof and member admission."""

import argparse
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from kohakutpu.application.runtime.device import Device
from kohakutpu.stack import rs


def timed(function, *args, **kwargs):
    started = time.perf_counter()
    result = function(*args, **kwargs)
    return result, time.perf_counter() - started


def measure(library, capture, cap):
    device = Device(kind="offline", board=capture["board"])
    machine = rs.machine_json(device.machine)
    device.close()
    source = next(iter(capture["sources"].values()))["source"]
    key = json.loads(rs._rs.library_key(source, machine))
    directory = library / key["machine"]
    if not directory.is_dir():
        raise RuntimeError(f"missing current-machine library {directory}")
    members = defaultdict(list)
    for path in sorted((directory / "dispatch").glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for member, entry in doc["members"].items():
            if member not in capture["sources"]:
                continue
            for variant in [entry, *entry.get("alternatives", [])]:
                for mesh, package in variant["packages"].items():
                    members[package["template"]["id"]].append(
                        (member, int(mesh), package)
                    )
    classifications = {}
    byte_seconds, bind_seconds, admission_seconds = 0.0, 0.0, 0.0
    compared, proven_members = 0, set()
    for path in sorted((directory / "classes").glob("*.json")):
        if path.name == "index.json":
            continue
        identifier = path.stem
        entry = json.loads(path.read_text(encoding="utf-8"))
        raw, table, bindings = (
            bytes.fromhex(entry["package"]),
            entry["table"],
            entry["bindings"],
        )
        verdict_text, proof_seconds = timed(
            rs._rs.template_verify,
            {0: raw},
            {0: table},
            {0: bindings},
            machine,
            identifier,
            mode="proof",
            cap=cap,
        )
        verdict = json.loads(verdict_text)
        sample_error = None
        try:
            samples, sample_seconds = timed(
                rs._rs.template_verify,
                {0: raw},
                {0: table},
                {0: bindings},
                machine,
                identifier,
                confined=True,
            )
            sample_points = len(json.loads(samples))
        except ValueError as error:
            sample_seconds, sample_points, sample_error = None, 0, str(error)
        classifications[identifier] = {
            "verdict": verdict,
            "proof_seconds": proof_seconds,
            "sample_seconds": sample_seconds,
            "sample_points": sample_points,
            "sample_error": sample_error,
            "captured_members": len(members[identifier]),
        }
        if verdict["status"] != "PROVEN":
            continue
        capability, seconds = timed(
            rs._rs.template_certify, raw, table, bindings, machine, cap
        )
        admission_seconds += seconds
        for member, mesh, metadata in members[identifier]:
            output = rs._rs.library_get(
                str(library), capture["sources"][member]["source"], machine
            )
            if output is None:
                raise RuntimeError(f"missing exact member {member}")
            direct = output["packages"][mesh]["bytes"]
            params = metadata["template"]["params"]
            member_table = dict(
                json.loads(table), buffer_sizes=metadata["buffer_sizes"]
            )
            _, seconds = timed(
                rs._rs.template_prove,
                raw,
                json.dumps(member_table),
                direct,
                json.dumps(params),
                bindings,
                machine,
                member,
                mesh,
            )
            byte_seconds += seconds
            bound, seconds = timed(capability.bind, json.dumps(params))
            bind_seconds += seconds
            assert bound == rs._rs.bind(direct, bindings, machine, mesh, False)
            compared += 1
            proven_members.add(member)
        print(f"{identifier}: PROVEN ({proof_seconds:.4f}s)", flush=True)
    counts = Counter(row["verdict"]["status"] for row in classifications.values())
    reasons = Counter(
        row["verdict"]["refusal"]["property"]
        for row in classifications.values()
        if row["verdict"]["status"] == "REFUSED"
    )
    return {
        "board": capture["board"],
        "batch": capture["batch"],
        "cap": cap,
        "templates": len(classifications),
        "counts": dict(counts),
        "refusals_by_property": dict(reasons),
        "source_count": len(capture["sources"]),
        "launch_count": len(capture["launches"]),
        "proven_source_candidates": len(proven_members),
        "proven_launch_candidates": sum(
            member in proven_members for member in capture["launches"]
        ),
        "compared_member_variants": compared,
        "member_byte_proof_seconds": byte_seconds,
        "capability_bind_seconds": bind_seconds,
        "capability_admission_seconds": admission_seconds,
        "proof_seconds": sum(row["proof_seconds"] for row in classifications.values()),
        "sample_seconds": sum(
            row["sample_seconds"] or 0 for row in classifications.values()
        ),
        "classifications": classifications,
        "scope": "L0 checker-safety candidates at fixed class extents/addresses; not graph-equivalence admission",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cap", type=int, default=2048)
    args = parser.parse_args()
    capture = json.loads(args.capture.read_text(encoding="utf-8"))
    result = measure(args.library, capture, args.cap)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "classifications"}))


if __name__ == "__main__":
    main()
