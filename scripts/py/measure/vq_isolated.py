"""Compile stream alternatives and execute one kernel per model session."""

import argparse
import copy
import hashlib
import json
import pickle
import time
from pathlib import Path

import numpy as np
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import rs
from vq_census import census
from vq_model import ROOT, model_counters, packed_reference
from vq_workload import walk


def compile_artifact(args):
    bundle = pickle.loads(args.artifact.read_bytes())
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    case = next(c for c in bundle["cases"] if c["label"] == args.kernel)
    graph = copy.deepcopy(case["graph"])
    for node in walk(graph["body"]):
        if node.get("op") == "quant":
            options = {
                "pipeline": 1,
                "zero_classifier": 1,
                "stream_axes": args.axes,
                "fill_overlap": args.fill,
            }
            node["kw"] = [p for p in node["kw"] if p[0] not in options]
            node["kw"] += [[k, {"int": v}] for k, v in options.items()]
    result = rs._rs.compile(
        json.dumps(graph), rs.machine_json(machine), cache_dir="", fetch=True
    )
    assert not result["verdict"]["refused"], result["verdict"]
    case.update(graph=graph, compiled=result)
    ports = {p["name"]: p["spec"] for p in graph["ports"]}
    shape = ports["x"]["shape"]
    source = (
        ((np.arange(np.prod(shape), dtype=np.int64) % 997 - 498) / 64)
        .astype("<f2")
        .reshape(shape)
    )
    target = np.zeros(ports["q"]["shape"], dtype="<f2")
    for node in walk(graph["body"]):
        if node.get("op") == "quant":
            src_axes = node["src"]["axes"]
            dst_axes = node["dst"]["axes"]
            sr, nr = src_axes[0]["run"]
            sc, nc = src_axes[1]["run"]
            dr = dst_axes[0]["run"][0]
            dc = 0 if dst_axes[1] == "all" else dst_axes[1]["run"][0]
            target[dr : dr + nr, dc : dc + nc] = source[sr : sr + nr, sc : sc + nc]
    layout = {k: v["int"] for k, v in ports["q"]["layout"]}
    side = ports["q"]["dtype"][-1]
    expected = packed_reference(
        target, side, layout["gm" if side == "a" else "gn"], layout["nk"]
    )
    case.update(source=source.tobytes(), expected=expected)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(pickle.dumps(case))
    print(
        json.dumps(
            {
                "kernel": args.kernel,
                "axes": args.axes,
                "fill": args.fill,
                "work": census(result["l1"]),
            }
        )
    )


def execute(args):
    case = pickle.loads(args.artifact.read_bytes())
    started = time.monotonic()
    log = args.output.with_suffix(".log")
    dev = D.Device(
        kind="sim",
        board="vlt_v9t6s_1n",
        nodes=1,
        build=ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
        log=log,
        classes=False,
    )
    dev.machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    records = []
    try:
        addresses = {
            base: dev.alloc(a["extent"] + 256)
            for base, a in case["allocations"].items()
        }
        binds = {
            name: addresses[v["allocation"]] + 128 + v["offset"]
            for name, v in case["views"].items()
        }
        for base, a in case["allocations"].items():
            dev.write(
                addresses[base],
                bytes([0xD3]) * 128
                + a["data"]
                + bytes(a["extent"] - len(a["data"]))
                + bytes([0xD3]) * 128,
            )
        dev.write(binds["x"], case["source"])
        compiled = generated.from_output(dev, case["graph"], case["compiled"])
        for _ in range(args.repeat):
            result = dev.run(compiled, binds, kind=case["kind"])
            output = dev.read(binds["q"], len(case["expected"]))
            assert output == case["expected"], case["label"]
            assert dev.read(binds["x"], len(case["source"])) == case["source"]
            for base, a in case["allocations"].items():
                assert (
                    dev.read(addresses[base], 128)
                    == dev.read(addresses[base] + 128 + a["extent"], 128)
                    == bytes([0xD3]) * 128
                )
            records.append(
                {
                    "cycles": result.cycles,
                    "parity": "independent bytes",
                    "sha256": hashlib.sha256(output).hexdigest(),
                }
            )
        for address in addresses.values():
            dev.free(address)
    finally:
        dev.close()
        counters = model_counters(log)
        work = census(case["compiled"]["l1"])
        actual = next(iter(counters.values()))["VRES"]
        checks = {
            k: {
                "census": work[k] * len(records),
                "model": actual["lanes" if k == "lane_slots" else k],
            }
            for k in ("fill_words", "drain_words", "lane_slots")
        }
        document = {
            "kernel": case["label"],
            "records": records,
            "counters": counters,
            "work": work,
            "checks": checks,
            "wall_seconds": time.monotonic() - started,
        }
        args.output.write_text(json.dumps(document, indent=2), encoding="utf-8")
        print(json.dumps(document))
        assert all(v["census"] == v["model"] for v in checks.values()), checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("compile", "execute"))
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kernel")
    parser.add_argument("--axes", type=int, default=0)
    parser.add_argument("--fill", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()
    (compile_artifact if args.mode == "compile" else execute)(args)


if __name__ == "__main__":
    main()
