"""Compile bounded Loop5 zero-image, independent-band and native-generator witnesses."""

import argparse
import copy
import json
import os
import pickle
from pathlib import Path

import numpy as np
from vq_census import census
from vq_model import ROOT, dsl_cases, packed_reference
from vq_workload import walk

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "group", choices=("zero", "dsl", "native", "validation", "tail")
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--value", type=int, default=0)
    parser.add_argument("--select", default="")
    args = parser.parse_args()
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    cases = []
    if args.group == "validation":
        for name in ("zero-all", "band1", "band4", "native0", "native1"):
            cases.extend(
                pickle.loads((args.output.parent / f"{name}.pkl").read_bytes())["cases"]
            )
        negative = pickle.loads((ROOT / ".plan/vq/loop4/negative.pkl").read_bytes())
        for case in negative["cases"]:
            for node in walk(case["graph"]["body"]):
                if node.get("op") == "quant":
                    node["kw"].append(["zero_reuse", {"int": 1}])
            case["compiled"] = rs._rs.compile(
                json.dumps(case["graph"]),
                rs.machine_json(machine),
                cache_dir="",
                fetch=True,
            )
            assert not case["compiled"]["verdict"]["refused"], case["compiled"][
                "verdict"
            ]
        args.output.with_name("negative.pkl").write_bytes(pickle.dumps(negative))
        args.output.write_bytes(
            pickle.dumps(dict(machine=machine.machine_json, cases=cases))
        )
        return
    if args.group == "zero":
        bundle = pickle.loads((ROOT / ".plan/vq/loop4/quant-witness.pkl").read_bytes())
        for original in bundle["cases"]:
            if args.select and args.select not in original["label"]:
                continue
            case = copy.deepcopy(original)
            for node in walk(case["graph"]["body"]):
                if node.get("op") == "quant":
                    node["kw"] = [p for p in node["kw"] if p[0] != "zero_reuse"]
                    node["kw"].append(["zero_reuse", {"int": args.value}])
            case["label"] += f"-zero{args.value}"
            cases.append(case)
    elif args.group == "dsl":
        for case in dsl_cases():
            if "class1" not in case["label"] or "wide" in case["label"]:
                continue
            if args.select and args.select not in case["label"]:
                continue
            kernel, given = case.pop("kernel"), case.pop("given")
            case["graph"] = kernel.trace(given, machine=K.dsl_machine(machine)).graph
            for node in walk(case["graph"]["body"]):
                if node.get("classed"):
                    node["class_band"] = args.value
            case["label"] += f"-band{args.value}"
            cases.append(case)
    elif args.group == "tail":
        for band in (1, 2, 4, 8):
            os.environ["KTPU_CLASS_BAND"] = str(band)
            for reduction, dtype in ((False, "f16"), (True, "f16")):
                rows, width, pitch, offset = 33, 96, 128, 16
                source = np.full(
                    (34, pitch), np.nan, dtype="<f4" if dtype == "f32" else "<f2"
                )
                live = (
                    ((np.arange(rows * width) % 16 - 8) / 16)
                    .astype(source.dtype)
                    .reshape(rows, width)
                )
                source[:rows, offset : offset + width] = live
                expr = (
                    X.Reduce("sum", X.Load(0))
                    if reduction
                    else X.op("max", X.Load(0), 0.0)
                )
                out = V.Operand("row") if reduction else V.Operand("full", pitch=width)
                kernel, given, _ = V.rows_kernel(
                    expr,
                    (V.Operand("full", pitch=pitch, offset=offset, dtype=dtype),),
                    rows,
                    width,
                    out,
                    1,
                    shape_class=ShapeClass(32, 256),
                )
                graph = kernel.trace(given, machine=K.dsl_machine(machine)).graph
                if reduction:
                    expected = np.zeros(48, dtype="<f2")
                    expected[:rows] = live.astype(np.float32).sum(-1)
                else:
                    expected = np.maximum(live, 0).astype("<f2")
                cases.append(
                    dict(
                        label=f"tail-{dtype}-reduce{int(reduction)}-band{band}",
                        kind="dsl",
                        shape=[rows, width],
                        graph=graph,
                        inputs=dict(in0=source.tobytes()),
                        output="out",
                        expected=expected.tobytes(),
                    )
                )
    else:
        for side, rows, cols, rp, cp, pitch, offset, batches in (
            ("a", 64, 32, 64, 32, 32, 0, 1),
            ("b", 5, 48, 8, 64, 64, 16, 2),
        ):
            source = (
                ((np.arange((batches * rows + 1) * pitch) % 997 - 498) / 64)
                .astype("<f2")
                .reshape(-1, pitch)
            )
            source[:, :offset] = np.nan
            source[:, offset + cols :] = np.nan
            padded = np.zeros((batches * rp, cp), dtype="<f2")
            for batch in range(batches):
                padded[batch * rp : batch * rp + rows, :cols] = source[
                    batch * rows : batch * rows + rows, offset : offset + cols
                ]
            parameters = dict(
                src=dict(kind="full", pitch=pitch, offset=offset, dtype="f16", rows=0),
                rows=rows,
                cols=cols,
                rp=rp,
                cp=cp,
                side=side,
                g=rp // 4,
                nk=1,
                batches=batches,
                schedule=dict(
                    pipeline=args.value,
                    zero_classifier=args.value,
                    stream_axes=args.value,
                    fill_overlap=args.value,
                    zero_reuse=args.value,
                ),
            )
            graph = json.loads(
                rs._rs.generate(
                    "matmul.quant_kernel",
                    json.dumps(parameters),
                    rs.machine_json(machine),
                    None,
                )
            )
            n = graph["ports"][0]["spec"]["shape"][0]
            cases.append(
                dict(
                    label=f"native-{side}-schedule{args.value}",
                    graph=graph,
                    kind="quant",
                    shape=[rows, cols],
                    inputs=dict(x=source[:n].tobytes()),
                    output="q",
                    expected=packed_reference(padded, side, rp // 4, 1),
                    read_bytes=0,
                    write_bytes=0,
                    math_lane_ops=0,
                )
            )
    for case in cases:
        result = rs._rs.compile(
            json.dumps(case["graph"]),
            rs.machine_json(machine),
            cache_dir="",
            fetch=True,
        )
        assert not result["verdict"]["refused"], result["verdict"]
        case["compiled"] = result
        work = census(result["l1"])
        case.update(
            read_bytes=work["fill_words"] * 32,
            write_bytes=work["drain_words"] * 32,
            math_lane_ops=work["lane_slots"],
        )
        print(json.dumps(dict(label=case["label"], work=work)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(
        pickle.dumps(dict(machine=machine.machine_json, cases=cases))
    )


if __name__ == "__main__":
    main()
