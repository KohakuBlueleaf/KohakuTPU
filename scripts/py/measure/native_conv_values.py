"""Bounded LeNet b8 conv2d and QKV-layout projection model comparisons."""

import argparse
import json
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
from kohakutpu.application import api as kp
from kohakutpu.application.api import native
from kohakutpu.application.kernels import transpose as T
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated as G
from kohakutpu.stack import rs
from native_values import decode_bytes, encode_bytes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prebuild", "sim"))
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--board", default="vlt_v9t6s_1n")
    args = parser.parse_args()
    if args.mode == "sim":
        shutil.unpack_archive(str(args.archive), str(args.library), "zip")
    dev = D.Device(
        "offline" if args.mode == "prebuild" else "sim",
        board=args.board,
        nodes=1,
        library=args.library,
        library_only=True,
    )
    captured, sources = {}, {}
    if args.mode == "sim":
        captured = json.loads(
            (args.library / "recorded.json").read_text(), object_hook=decode_bytes
        )
        sources = json.loads((args.library / "graphs.json").read_text())
    original, original_graph = dev.compile_graph, G.graph

    def lookup(source):
        machine = rs.machine_json(dev.machine)
        key = json.loads(rs._rs.library_key(source, machine))["class"]
        if args.mode == "prebuild":
            rs._rs.library_prebuild(str(args.library), source, machine)
            captured[key] = original(source)
        output = captured[key]
        output["packages"] = {int(k): v for k, v in output["packages"].items()}
        return output

    def generate(generator, arguments, machine, mutant):
        if generator == "matmul.mm_kernel":
            parsed = json.loads(arguments)
            m_args = parsed["mm"]["made_by"]["args"]
            m_args.setdefault("pad_m", False)
            m_args.setdefault("dense_n", False)
            arguments = json.dumps(parsed, sort_keys=True)
        key = json.dumps([generator, arguments, machine, mutant])
        if args.mode == "prebuild":
            sources[key] = original_graph(generator, arguments, machine, mutant)
        return sources[key]

    def forbidden(*a, **kw):
        raise AssertionError("runtime compiler/generator reached")

    rng = np.random.default_rng(43)
    report = {"mode": args.mode, "package_replay": args.mode == "sim", "ops": {}}
    with (
        D.attached(dev),
        patch.object(dev, "compile_graph", lookup),
        patch.object(G, "graph", generate),
        patch.object(
            rs._rs, "compile", rs._rs.compile if args.mode == "prebuild" else forbidden
        ),
    ):
        # 1. LeNet c2 b8: [8, 14, 14, 16], kernel 5x5, pad 0 -> [8, 10, 10, 32], ReLU
        x2_arr = rng.normal(0, 0.2, (8, 14, 14, 16)).astype(np.float16)
        w2_arr = rng.normal(0, 0.05, (32, 416)).astype(np.float16)
        w2_arr[16:, :] = 0
        w2_arr[:, 400:] = 0
        b2_arr = np.zeros(32, np.float16)
        b2_arr[:16] = rng.normal(0, 0.05, 16).astype(np.float16)
        w2, b2 = kp.tensor(w2_arr), kp.tensor(b2_arr)

        # 2. LeNet c1 b8: [8, 28, 28, 16], kernel 5x5, pad 2 -> [8, 28, 28, 32], ReLU
        x1_arr = np.zeros((8, 28, 28, 16), np.float16)
        x1_arr[:, :, :, :1] = rng.normal(0, 0.2, (8, 28, 28, 1)).astype(np.float16)
        w1_arr = np.zeros((32, 416), np.float16)
        w1_arr[:6, :400] = rng.normal(0, 0.05, (6, 400)).astype(np.float16)
        b1_arr = np.zeros(32, np.float16)
        b1_arr[:6] = rng.normal(0, 0.05, 6).astype(np.float16)
        w1, b1 = kp.tensor(w1_arr), kp.tensor(b1_arr)

        # 3. QKV projection writing planar attention layout: [1, 16, 192]
        xq_arr = rng.normal(0, 0.2, (1, 16, 192)).astype(np.float16)
        scale = kp.tensor((1 + rng.normal(0, 0.02, 192)).astype(np.float16))
        shift = kp.tensor(rng.normal(0, 0.02, 192).astype(np.float16))
        wq = kp.tensor(rng.normal(0, 0.02, (576, 192)).astype(np.float16))
        bq = kp.tensor(rng.normal(0, 0.02, 576).astype(np.float16))
        mask = kp.tensor(T.masks())

        # 4. LeNet c2 backward dX b8: [8, 10, 10, 32], kernel 5x5, pad 4 -> [8, 14, 14, 32]
        dy2_arr = rng.normal(0, 0.2, (8, 10, 10, 32)).astype(np.float16)
        wb2_arr = rng.normal(0, 0.05, (32, 800)).astype(np.float16)
        zb2_arr = np.zeros(32, np.float16)
        wb2, zb2 = kp.tensor(wb2_arr), kp.tensor(zb2_arr)

        # 5. LeNet b8 dense head forward + backward dX: [8, 416] -> [8, 128] -> [8, 96] -> [8, 32]
        xh_arr = rng.normal(0, 0.2, (8, 416)).astype(np.float16)
        wl1 = kp.tensor(rng.normal(0, 0.05, (128, 416)).astype(np.float16))
        bl1 = kp.tensor(rng.normal(0, 0.05, (128,)).astype(np.float16))
        wl2 = kp.tensor(rng.normal(0, 0.05, (96, 128)).astype(np.float16))
        bl2 = kp.tensor(rng.normal(0, 0.05, (96,)).astype(np.float16))
        wl3 = kp.tensor(rng.normal(0, 0.05, (32, 96)).astype(np.float16))
        bl3 = kp.tensor(rng.normal(0, 0.05, (32,)).astype(np.float16))
        dlog_arr = rng.normal(0, 0.2, (8, 32)).astype(np.float16)
        wl3_t = kp.tensor(rng.normal(0, 0.05, (96, 32)).astype(np.float16))
        z96 = kp.tensor(np.zeros(96, np.float16))
        wl2_t = kp.tensor(rng.normal(0, 0.05, (128, 96)).astype(np.float16))
        z128 = kp.tensor(np.zeros(128, np.float16))
        wl1_t = kp.tensor(rng.normal(0, 0.05, (416, 128)).astype(np.float16))
        z416 = kp.tensor(np.zeros(416, np.float16))

        def lenet_head_step(f):
            opts = {"fused": f, "backend": "generated", "ring_depth": 2}
            h = native.mlp(
                kp.tensor(xh_arr), wl1, bl1, wl2, bl2, activation="relu", **opts
            )
            logits = native.linear(h, wl3, bl3, **opts)
            dh2 = native.linear(kp.tensor(dlog_arr), wl3_t, z96, **opts)
            dx = native.mlp(dh2, wl2_t, z128, wl1_t, z416, activation="none", **opts)
            return logits, dx

        cases = [
            (
                "lenet_c2_b8",
                lambda f: (
                    native.conv2d(
                        kp.tensor(x2_arr),
                        w2,
                        b2,
                        padding=0,
                        activation="relu",
                        fused=f,
                    ),
                ),
            ),
            (
                "lenet_c1_b8",
                lambda f: (
                    native.conv2d(
                        kp.tensor(x1_arr),
                        w1,
                        b1,
                        padding=2,
                        activation="relu",
                        fused=f,
                    ),
                ),
            ),
            (
                "lenet_c2_bwd_dx_b8",
                lambda f: (
                    native.conv2d(
                        kp.tensor(dy2_arr),
                        wb2,
                        zb2,
                        padding=4,
                        activation="none",
                        fused=f,
                    ),
                ),
            ),
            ("lenet_head_fwd_bwd_b8", lenet_head_step),
            (
                "norm_qkv_w192_t16",
                lambda f: native.norm_qkv(
                    kp.tensor(xq_arr), scale, shift, wq, bq, mask, eps=1e-6, fused=f
                ),
            ),
        ]
        for name, fn in cases:
            samples, values = [], []
            for fused in (False, True):
                fn(fused)
                dev.drain()
                mark, start = len(dev.runs), time.perf_counter()
                outs = fn(fused)
                dev.drain()
                runs = dev.runs[mark:]
                samples.append(
                    {
                        "fused": fused,
                        "launches": len(runs),
                        "cycles": sum(r.cycles for r in runs),
                        "seconds": time.perf_counter() - start,
                        "records": [
                            {"name": r.name, "kind": r.kind, "cycles": r.cycles}
                            for r in runs
                        ],
                    }
                )
                if args.mode == "sim":
                    values.append([o.numpy().astype(np.float32) for o in outs])
            entry = {"variants": samples}
            if values:
                diffs = [a - b for a, b in zip(values[1], values[0], strict=True)]
                entry["numerics"] = {
                    "finite": bool(
                        all(np.isfinite(v).all() for group in values for v in group)
                    ),
                    "max_absolute": float(max(np.abs(d).max() for d in diffs)),
                    "rms": float(max(np.sqrt(np.mean(d * d)) for d in diffs)),
                    "peak": float(max(np.abs(v).max() for v in values[0])),
                }
            report["ops"][name] = entry
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2))
            print(name, json.dumps(entry), flush=True)
            if values:
                for a, b in zip(values[1], values[0], strict=True):
                    np.testing.assert_allclose(a, b, atol=1e-3, rtol=1e-2, err_msg=name)
    if args.mode == "prebuild":
        (args.library / "recorded.json").write_text(
            json.dumps(captured, default=encode_bytes)
        )
        (args.library / "graphs.json").write_text(json.dumps(sources))
        shutil.make_archive(str(args.archive.with_suffix("")), "zip", args.library)


if __name__ == "__main__":
    main()
