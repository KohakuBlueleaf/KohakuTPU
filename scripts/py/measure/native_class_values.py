"""Execute a certified ring class at two shapes and two relocated address sets."""

import argparse
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import numpy as np
from kohakutpu.application import api as kp
from kohakutpu.application.api.custom import call
from kohakutpu.application.kernels.native import generated as G
from kohakutpu.application.kernels.native.composition import GraphKernel
from kohakutpu.application.kernels.native.library import admit_pair
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.stack import rs
from native_values import decode_bytes, encode_bytes


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("prebuild", "sim"))
    p.add_argument("--library", type=Path, required=True)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.mode == "sim":
        shutil.unpack_archive(str(args.archive), str(args.library), "zip")
    dev = D.Device(
        "offline" if args.mode == "prebuild" else "sim",
        board="vlt_v9t6s_1n",
        library=args.library,
        library_only=True,
        classes=True,
    )
    machine = rs.machine_json(dev.machine)
    if args.mode == "prebuild":
        graphs = [
            G.linear(m, 64, 32, dev.machine, "relu", row_capacity=16).graph
            for m in (8, 16)
        ]
        admit_pair(args.library, machine, graphs)
        classes = [
            ClassLibrary(args.library, machine).lookup(json.dumps(g)) for g in graphs
        ]
        data = {"graphs": graphs, "classes": classes, "exact": {}}
    else:
        data = json.loads(
            (args.library / "class-record.json").read_text(), object_hook=decode_bytes
        )
        graphs = data["graphs"]
    original = dev.compile_graph

    def lookup(source):
        key = json.loads(rs._rs.library_key(source, machine))["class"]
        if args.mode == "prebuild":
            if source not in [json.dumps(g) for g in graphs]:
                rs._rs.library_prebuild(str(args.library), source, machine)
            output = original(source)
            data["exact"][key] = output
            return output
        for g, output in zip(graphs, data["classes"], strict=True):
            if json.loads(rs._rs.library_key(json.dumps(g), machine))["class"] == key:
                output["packages"] = {int(k): v for k, v in output["packages"].items()}
                return output
        output = data["exact"][key]
        output["packages"] = {int(k): v for k, v in output["packages"].items()}
        return output

    def forbidden(*a, **kw):
        raise AssertionError("runtime compiler reached")

    report = {"mode": args.mode, "variants": []}
    rng = np.random.default_rng(73)
    weight = rng.normal(0, 0.1, (32, 64)).astype(np.float16)
    bias = rng.normal(0, 0.1, 32).astype(np.float16)
    held = []
    with (
        D.attached(dev),
        patch.object(dev, "compile_graph", lookup),
        patch.object(
            rs._rs, "compile", rs._rs.compile if args.mode == "prebuild" else forbidden
        ),
    ):
        w, b = kp.tensor(weight), kp.tensor(bias)
        for rows, g in zip((8, 16), graphs, strict=True):
            array = rng.normal(0, 0.1, (rows, 64)).astype(np.float16)
            for address_set in range(2):
                x = kp.tensor(array)
                reference = (x @ w.T + b).realize().relu().realize()
                mark = len(dev.runs)
                result = call(GraphKernel(g), x, w, b)[0]
                held.extend((x, reference, result))
                dev.drain()
                row = {
                    "rows": rows,
                    "address_set": address_set,
                    "x_address": x.st.addr,
                    "y_address": result.st.addr,
                    "cycles": sum(r.cycles for r in dev.runs[mark:]),
                }
                if args.mode == "sim":
                    a, z = result.numpy(), reference.numpy()
                    row["max_absolute"] = float(
                        np.max(np.abs(a.astype(np.float32) - z))
                    )
                    np.testing.assert_array_equal(a, z)
                report["variants"].append(row)
    report["counters"] = dict(dev.counters)
    args.output.write_text(json.dumps(report, indent=2))
    if args.mode == "prebuild":
        (args.library / "class-record.json").write_text(
            json.dumps(data, default=encode_bytes)
        )
        shutil.make_archive(str(args.archive.with_suffix("")), "zip", args.library)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
