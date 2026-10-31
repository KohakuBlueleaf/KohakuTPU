"""One bounded native MLP numerical probe; prebuild on host, execute on model."""

import argparse
import json
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
from kohakutpu.application import api as kp
from kohakutpu.application.api import native
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated as G
from kohakutpu.stack import rs


def encode_bytes(value):
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    raise TypeError(type(value).__name__)


def decode_bytes(value):
    return bytes.fromhex(value["$bytes"]) if "$bytes" in value else value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prebuild", "lookup", "sim"))
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument(
        "--package-replay",
        action="store_true",
        help="Measurement-only replay of host compiler outputs; not library admission",
    )
    parser.add_argument("--rows", type=int, default=8)
    parser.add_argument("--width", type=int, default=64)
    parser.add_argument("--hidden", type=int, default=64)
    parser.add_argument("--columns", type=int, default=32)
    parser.add_argument("--class-capacity", type=int, choices=(0, 1), default=1)
    parser.add_argument("--warm", action="store_true")
    parser.add_argument("--backend", choices=("class", "generated"), default="class")
    parser.add_argument("--ring-depth", type=int, choices=(0, 1, 2), default=2)
    parser.add_argument("--activation", choices=("relu", "gelu"), default="relu")
    args = parser.parse_args()
    if args.mode == "sim" and args.archive:
        shutil.unpack_archive(str(args.archive), str(args.library), "zip")
    dev = D.Device(
        "sim" if args.mode == "sim" else "offline",
        board="vlt_v9t6s_1n",
        nodes=1,
        library=args.library,
        library_only=True,
    )
    original = dev.compile_graph
    native_prebuild = rs._rs.library_prebuild
    captured = {}
    sources = {}
    native_graph = G.graph
    if args.package_replay:
        sources = json.loads((args.library / "graphs.json").read_text())

    def generate(generator, arguments, machine, mutant):
        key = json.dumps([generator, arguments, machine, mutant])
        if args.mode == "prebuild":
            sources[key] = native_graph(generator, arguments, machine, mutant)
        return sources[key]

    if args.package_replay:
        captured = json.loads(
            (args.library / "recorded.json").read_text(), object_hook=decode_bytes
        )

    def replay(source):
        key = json.loads(rs._rs.library_key(source, rs.machine_json(dev.machine)))
        output = captured[key["class"]]
        output["packages"] = {int(k): v for k, v in output["packages"].items()}
        return output

    def build(source):
        native_prebuild(str(args.library), source, rs.machine_json(dev.machine))
        output = original(source)
        key = json.loads(rs._rs.library_key(source, rs.machine_json(dev.machine)))
        captured[key["class"]] = output
        return output

    def forbidden(*a, **kw):
        raise AssertionError("runtime compiler reached during library-only model run")

    rng = np.random.default_rng(71)
    arrays = [
        (rng.standard_normal(s) * 0.1).astype(np.float16)
        for s in (
            (args.rows, args.width),
            (args.hidden, args.width),
            (args.hidden,),
            (args.columns, args.hidden),
            (args.columns,),
        )
    ]
    report = {
        "mode": args.mode,
        "warm": args.warm,
        "backend": args.backend,
        "ring_depth": args.ring_depth,
        "activation": args.activation,
        "class_capacity": (
            bool(args.class_capacity) if args.backend == "class" else None
        ),
        "package_replay": args.package_replay,
        "shape": [args.rows, args.width, args.hidden, args.columns],
        "variants": [],
    }
    values = []
    with (
        D.attached(dev),
        patch.object(
            G,
            "graph",
            (
                generate
                if args.mode == "prebuild" or args.package_replay
                else native_graph
            ),
        ),
        patch.object(
            dev,
            "compile_graph",
            (
                build
                if args.mode == "prebuild"
                else replay if args.package_replay else original
            ),
        ),
        patch.object(
            rs._rs, "compile", rs._rs.compile if args.mode == "prebuild" else forbidden
        ),
        patch.object(
            rs._rs,
            "library_prebuild",
            native_prebuild if args.mode == "prebuild" else forbidden,
        ),
    ):
        tensors = [kp.tensor(a) for a in arrays]
        for fused in (False, True):
            mark = len(dev.runs)
            started = time.perf_counter()
            if args.warm:
                native.mlp(
                    *tensors,
                    activation=args.activation,
                    backend=args.backend,
                    ring_depth=args.ring_depth,
                    fused=fused,
                    class_capacity=bool(args.class_capacity),
                )
                dev.drain()
                tensors[0] = kp.tensor(arrays[0])
                mark = len(dev.runs)
                started = time.perf_counter()
            result = native.mlp(
                *tensors,
                activation=args.activation,
                backend=args.backend,
                ring_depth=args.ring_depth,
                fused=fused,
                class_capacity=bool(args.class_capacity),
            )
            if args.mode == "sim":
                values.append(result.numpy().astype(np.float32))
            dev.drain()
            runs = dev.runs[mark:]
            report["variants"].append(
                {
                    "fused": fused,
                    "launches": len(runs),
                    "cycles": sum(r.cycles for r in runs),
                    "records": [
                        {"name": r.name, "kind": r.kind, "cycles": r.cycles}
                        for r in runs
                    ],
                    "seconds": time.perf_counter() - started,
                }
            )
        report["counters"] = dict(dev.counters)
    if values:
        delta = values[1] - values[0]
        report["numerics"] = {
            "max_absolute": float(np.abs(delta).max()),
            "rms": float(np.sqrt(np.mean(delta * delta))),
            "unfused_peak": float(np.abs(values[0]).max()),
            "finite": bool(all(np.isfinite(v).all() for v in values)),
            "outputs": [v.tolist() for v in values],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    if args.mode == "prebuild":
        (args.library / "graphs.json").write_text(json.dumps(sources))
        (args.library / "recorded.json").write_text(
            json.dumps(captured, default=encode_bytes)
        )
    if args.mode == "prebuild" and args.archive:
        shutil.make_archive(str(args.archive.with_suffix("")), "zip", args.library)
    print(json.dumps({k: v for k, v in report.items() if k != "numerics"}), flush=True)
    if values:
        assert report["numerics"]["finite"]
        np.testing.assert_allclose(values[1], values[0], atol=1e-3, rtol=1e-2)
        print(
            json.dumps({k: v for k, v in report["numerics"].items() if k != "outputs"}),
            flush=True,
        )


if __name__ == "__main__":
    main()
