"""Offline fused-kernel structure and supplied silicon-budget measurements."""

import argparse
import json
from pathlib import Path

from kohakutpu.application.kernels.native.linear import kernel, mlp_kernel
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def budget(batch=1, tokens=197, width=768, cycles=10_000_000):
    linear = 12 * batch * tokens * width * width
    attention = 2 * batch * tokens * tokens * width
    peak = 2048
    floor = (linear + attention) / peak
    return {
        "batch": batch,
        "tokens": tokens,
        "width": width,
        "linear_macs": linear,
        "attention_macs": attention,
        "linear_floor": linear / peak,
        "attention_floor": attention / peak,
        "old_rate_cycles": linear / (peak * 0.9) + attention / (peak * 0.28),
        "block_floor": floor,
        "block_cycles_for_28_percent": floor / 0.28,
        "block_mfu_at_supplied_cycles": floor / cycles,
        "optimistic_mfu_if_only_attention_free": floor / (cycles - 5_700_000),
        "optimistic_mfu_if_attention_and_fc2_quant_free": floor
        / (cycles - 5_700_000 - 780_000),
    }


def compile_case(kernel, given, machine):
    traced = kernel.trace(given, machine=K.dsl_machine(machine))
    output = rs._rs.compile(
        traced.graph_json, rs.machine_json(machine), cache_dir="", fetch=True
    )
    return {
        "name": kernel.name,
        "given": given,
        "verdict": output["verdict"],
        "package_bytes": {
            str(k): len(v["bytes"]) for k, v in output["packages"].items()
        },
        "package_steps": {
            str(k): len(
                rs._rs.package_read(v["bytes"], rs.machine_json(machine), k)["steps"]
            )
            for k, v in output["packages"].items()
        },
        "l1_chars": len(output["l1"]),
        "scratch": output.get("scratch"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path.home() / "ktpu/.plan/ovh/card-vit-w768-b1-mid-d2-selected.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"budget": budget(), "kernels": []}
    print(json.dumps({"budget": budget()}), flush=True)
    if args.baseline.exists():
        raw = json.loads(args.baseline.read_text())
        records = raw["records"][-407:]
        cfg = raw["configuration"]
        b, w, depth = cfg["batch"], cfg["width"], cfg["blocks"]
        tokens = (cfg["image_size"] // 16) ** 2 + 1
        block = budget(b, tokens, w)
        macs = (
            depth * (block["linear_macs"] + block["attention_macs"])
            + b * (tokens - 1) * 768 * w
            + b * w * 1000
        )
        busy = sum(r["cycles"] for r in records)
        report["baseline"] = {
            "configuration": cfg,
            "samples": raw["samples"],
            "forward_launches": len(records),
            "forward_busy_cycles": busy,
            "forward_macs": macs,
            "forward_busy_mfu": macs / (2048 * busy),
            "by_kind": {
                kind: {
                    "count": sum(r["kind"] == kind for r in records),
                    "cycles": sum(r["cycles"] for r in records if r["kind"] == kind),
                }
                for kind in sorted({r["kind"] for r in records})
            },
            "first_block_records": [
                dict(index=i, **r) for i, r in enumerate(records[5:39], 5)
            ],
        }
        print(
            json.dumps(
                {
                    "baseline": {
                        k: v
                        for k, v in report["baseline"].items()
                        if k not in ("samples", "first_block_records")
                    }
                }
            ),
            flush=True,
        )
    machine = OfflineCard("multimesh_v9t6").machine
    clusters, cores = machine.count("MG"), machine.count("VC")
    outputs = []
    for rows in (8, 16):
        k = kernel(
            rows,
            64,
            32,
            clusters,
            cores,
            "relu",
            True,
            8,
            machine_json=rs.machine_json(machine),
        )
        given = {"x": (rows, 64), "w": (32, 64), "bias": (32,), "residual": (rows, 32)}
        result = compile_case(k, given, machine)
        report["kernels"].append(result)
        print(json.dumps(result), flush=True)
        traced = k.trace(given, machine=K.dsl_machine(machine))
        output = rs._rs.compile(
            traced.graph_json, rs.machine_json(machine), cache_dir="", fetch=True
        )
        outputs.append(output["packages"][0]["bytes"])
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.with_suffix(f".m{rows}.l1").write_text(output["l1"])
            args.output.with_suffix(f".m{rows}.package.json").write_text(
                json.dumps(
                    rs._rs.package_read(outputs[-1], rs.machine_json(machine)), indent=2
                )
            )
    decoded = [rs._rs.package_read(raw, rs.machine_json(machine)) for raw in outputs]
    report["step_differences"] = [
        {"index": i, "left": a, "right": b}
        for i, (a, b) in enumerate(zip(decoded[0]["steps"], decoded[1]["steps"]))
        if a != b
    ]
    print(json.dumps({"step_differences": report["step_differences"]}), flush=True)
    try:
        derived = rs._rs.template_derive(
            outputs,
            json.dumps([{"member": 0}, {"member": 1}]),
            json.dumps({"member": {"lo": 0, "hi": 1}}),
            rs.machine_json(machine),
        )
        report["template"] = {
            "bytes": len(derived["bytes"]),
            "table": json.loads(derived["template_json"]),
        }
    except ValueError as error:
        report["template"] = {"refusal": str(error)}
    print(json.dumps({"template": report["template"]}), flush=True)
    k = mlp_kernel(
        8, 64, 64, 32, clusters, cores, machine_json=rs.machine_json(machine)
    )
    result = compile_case(
        k,
        {"x": (8, 64), "w1": (64, 64), "b1": (64,), "w2": (32, 64), "b2": (32,)},
        machine,
    )
    report["kernels"].append(result)
    print(json.dumps(result), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
