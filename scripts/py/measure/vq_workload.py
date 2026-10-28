"""Isolated full-shape capture replay; compare schedule variants, not a model output.

Compile mode runs on the host. Execute consumes only compiled immutable artifacts.
"""

import argparse
import copy
import hashlib
import json
import pickle
import time
from pathlib import Path

from vq_model import ROOT, model_counters

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import rs


def walk(nodes):
    for node in nodes:
        yield node
        yield from walk(node.get("body", []))


def compile_variants(args):
    data = pickle.loads(args.artifact.read_bytes())
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    for case in data["cases"]:
        graph = copy.deepcopy(case["graph"])
        changed = False
        for node in walk(graph["body"]):
            if node.get("op") == "quant":
                node["kw"] = [
                    p
                    for p in node["kw"]
                    if p[0] not in ("pipeline", "classifier_overlap")
                ]
                node["kw"].append(["pipeline", {"int": 1}])
                changed = True
        if changed:
            result = rs._rs.compile(
                json.dumps(graph), rs.machine_json(machine), cache_dir="", fetch=True
            )
            assert not result["verdict"]["refused"], (case["label"], result["verdict"])
            case["after"] = result
        else:
            case["after"] = case["compiled"]
        case["changed"] = changed
    args.output.write_bytes(pickle.dumps(data))


def execute(args):
    data = pickle.loads(args.artifact.read_bytes())
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
    assert dev.machine.machine_json == data["machine"]
    records = []
    try:
        for case in data["cases"]:
            if time.monotonic() - started > 510:
                break
            actuals, cycles = {}, {}
            addresses = {
                base: dev.alloc(a["extent"] + 256)
                for base, a in case["allocations"].items()
            }
            binds = {
                name: addresses[v["allocation"]] + 128 + v["offset"]
                for name, v in case["views"].items()
            }
            try:
                for variant in ("compiled", "after"):
                    if variant == "after" and not case["changed"]:
                        cycles[variant] = cycles["compiled"]
                        actuals[variant] = actuals["compiled"]
                        continue
                    for base, alloc in case["allocations"].items():
                        dev.write(
                            addresses[base],
                            bytes([0xD3]) * 128
                            + alloc["data"]
                            + bytes(alloc["extent"] - len(alloc["data"]))
                            + bytes([0xD3]) * 128,
                        )
                    comp = generated.from_output(dev, case["graph"], case[variant])
                    run = dev.run(comp, binds, kind=case["kind"])
                    cycles[variant] = run.cycles
                    outputs = {}
                    for base, alloc in case["allocations"].items():
                        prefix = dev.read(addresses[base], 128)
                        suffix = dev.read(addresses[base] + 128 + alloc["extent"], 128)
                        assert prefix == suffix == bytes([0xD3]) * 128, (
                            case["label"],
                            "guard",
                        )
                    for name, view in case["views"].items():
                        if view["role"] != "in":
                            outputs[name] = dev.read(binds[name], view["nbytes"])
                    actuals[variant] = {
                        name: hashlib.sha256(raw).hexdigest()
                        for name, raw in outputs.items()
                    }
                assert actuals["compiled"] == actuals["after"], case["label"]
                record = dict(
                    key=case["key"],
                    name=case["label"],
                    kind=case["kind"],
                    multiplicity=case["multiplicity"],
                    before=cycles["compiled"],
                    after=cycles["after"],
                    changed=case["changed"],
                    parity="exact" if case["changed"] else "unchanged-package",
                    output_sha256=actuals["compiled"],
                )
                records.append(record)
                print(json.dumps(record), flush=True)
                args.output.write_text(
                    json.dumps(dict(records=records), indent=2), encoding="utf-8"
                )
            finally:
                for address in addresses.values():
                    dev.free(address)
    finally:
        dev.close()
        args.output.write_text(
            json.dumps(
                dict(
                    model=data["model"],
                    total_classes=len(data["cases"]),
                    records=records,
                    wall_seconds=time.monotonic() - started,
                    counters=model_counters(log),
                ),
                indent=2,
            ),
            encoding="utf-8",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("compile", "execute"))
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    (compile_variants if args.mode == "compile" else execute)(args)


if __name__ == "__main__":
    main()
