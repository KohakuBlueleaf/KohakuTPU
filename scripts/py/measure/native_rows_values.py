"""Bounded norm, softmax and planar attention prebuild/model numerical probes."""

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
from kohakutpu.stack import rs
from native_values import decode_bytes, encode_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prebuild", "sim"))
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "sim":
        shutil.unpack_archive(str(args.archive), str(args.library), "zip")
    dev = D.Device(
        "offline" if args.mode == "prebuild" else "sim",
        board="vlt_v9t6s_1n",
        nodes=1,
        library=args.library,
        library_only=True,
    )
    captured = (
        {}
        if args.mode == "prebuild"
        else json.loads(
            (args.library / "recorded.json").read_text(), object_hook=decode_bytes
        )
    )
    original = dev.compile_graph

    def lookup(source):
        machine = rs.machine_json(dev.machine)
        key = json.loads(rs._rs.library_key(source, machine))["class"]
        if args.mode == "prebuild":
            rs._rs.library_prebuild(str(args.library), source, machine)
            captured[key] = original(source)
        output = captured[key]
        output["packages"] = {int(k): v for k, v in output["packages"].items()}
        return output

    def forbidden(*a, **kw):
        raise AssertionError("model runtime compiler reached")

    rng = np.random.default_rng(23)
    report = {"mode": args.mode, "package_replay": args.mode == "sim", "ops": {}}
    with (
        D.attached(dev),
        patch.object(dev, "compile_graph", lookup),
        patch.object(
            rs._rs, "compile", rs._rs.compile if args.mode == "prebuild" else forbidden
        ),
    ):
        x = kp.tensor(rng.normal(0, 0.2, (8, 192)).astype(np.float16))
        scale = kp.tensor(np.ones(192, np.float16))
        shift = kp.tensor(np.zeros(192, np.float16))
        q = kp.tensor(rng.normal(0, 0.2, (1, 16, 64)).astype(np.float16))
        k = kp.tensor(rng.normal(0, 0.2, (1, 32, 64)).astype(np.float16))
        vt = kp.tensor(rng.normal(0, 0.2, (1, 64, 32)).astype(np.float16))
        for op, inputs in (
            ("softmax", (x,)),
            ("layernorm", (x, scale, shift)),
            ("attention", (q, k, vt)),
        ):
            samples, values = [], []
            for fused in (False, True):
                start, mark = time.perf_counter(), len(dev.runs)
                y = getattr(native, op)(*inputs, fused=fused)
                if args.mode == "sim":
                    values.append(y.numpy().astype(np.float32))
                dev.drain()
                samples.append(
                    {
                        "fused": fused,
                        "cycles": sum(r.cycles for r in dev.runs[mark:]),
                        "launches": len(dev.runs) - mark,
                        "seconds": time.perf_counter() - start,
                    }
                )
            result = {"variants": samples}
            if values:
                delta = values[1] - values[0]
                result["numerics"] = {
                    "finite": bool(all(np.isfinite(v).all() for v in values)),
                    "max_absolute": float(np.abs(delta).max()),
                    "rms": float(np.sqrt(np.mean(delta * delta))),
                    "unfused_peak": float(np.abs(values[0]).max()),
                }
            report["ops"][op] = result
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2))
            print(op, json.dumps(result), flush=True)
            if values:
                assert result["numerics"]["finite"]
                np.testing.assert_allclose(values[1], values[0], atol=1e-3, rtol=1e-2)
    if args.mode == "prebuild":
        (args.library / "recorded.json").write_text(
            json.dumps(captured, default=encode_bytes)
        )
        shutil.make_archive(str(args.archive.with_suffix("")), "zip", args.library)


if __name__ == "__main__":
    main()
