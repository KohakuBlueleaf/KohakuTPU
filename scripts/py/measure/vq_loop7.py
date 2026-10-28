"""Compile stream-batch and direct compact-class experiments for isolated model runs."""

import argparse
import copy
import json
import os
import pickle
from pathlib import Path

from vq_census import census
from vq_model import ROOT, dsl_cases
from vq_workload import walk

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("quant", "dsl", "validation", "negative"))
    parser.add_argument("--value", type=int, required=True)
    parser.add_argument("--select", default="")
    parser.add_argument("--knob", default="stream_batch")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    cases = []
    if args.kind in ("quant", "validation", "negative"):
        origin = "negative" if args.kind == "negative" else "validation"
        originals = pickle.loads((ROOT / f".plan/vq/loop5/{origin}.pkl").read_bytes())[
            "cases"
        ]
        for original in originals:
            if (
                args.kind != "negative" and not original["label"].startswith("quant_")
            ) or (args.select and args.select not in original["label"]):
                continue
            case = copy.deepcopy(original)
            for node in walk(case["graph"]["body"]):
                if node.get("op") == "quant":
                    node["kw"] = [p for p in node["kw"] if p[0] != args.knob]
                    node["kw"].append([args.knob, {"int": args.value}])
                    if args.kind in ("validation", "negative"):
                        node["kw"] = [
                            p for p in node["kw"] if p[0] != "classifier_banks"
                        ]
                        node["kw"].append(["classifier_banks", {"int": 1}])
            if args.kind != "negative":
                case["label"] += f"-{args.knob}{args.value}"
            cases.append(case)
    else:
        os.environ["KTPU_CLASS_ROWS_DIRECT"] = str(args.value)
        for case in dsl_cases():
            if (
                "class1" not in case["label"]
                or "wide" in case["label"]
                or (args.select and args.select not in case["label"])
            ):
                continue
            kernel, given = case.pop("kernel"), case.pop("given")
            case["graph"] = kernel.trace(given, machine=K.dsl_machine(machine)).graph
            case["label"] += f"-rowsdirect{args.value}"
            cases.append(case)
    if args.kind == "validation":
        extras = pickle.loads((ROOT / ".plan/vq/loop7/d1.pkl").read_bytes())["cases"]
        cases.extend(extras)
    for case in cases:
        compiled = rs._rs.compile(
            json.dumps(case["graph"]),
            rs.machine_json(machine),
            cache_dir="",
            fetch=True,
        )
        assert not compiled["verdict"]["refused"], compiled["verdict"]
        case["compiled"] = compiled
        work = census(compiled["l1"])
        case.update(
            read_bytes=work.get("fill_words", 0) * 32,
            write_bytes=work.get("drain_words", 0) * 32,
            math_lane_ops=work.get("lane_slots", 0),
        )
        print(json.dumps(dict(label=case["label"], work=work)), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(
        pickle.dumps(dict(machine=machine.machine_json, cases=cases))
    )


if __name__ == "__main__":
    main()
