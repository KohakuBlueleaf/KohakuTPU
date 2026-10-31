"""Native automatic-capture artifact and production Device.replay model witness."""

import argparse
import functools
import json
import os
import pickle
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.card import VerilatorCard
from kohakutpu.application.runtime.graph.recorded import NativeRecording
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def forbidden(*args, **kwargs):
    raise AssertionError("compilation on replay")


def compile_artifact(path):
    dev = D.Device("offline", board="vlt_v9t6s_1n")
    rows, width = 16, 128
    kernels = []
    for e in (X.op("add", X.Load(0), 0.125), X.op("max", X.Load(0), 0)):
        kernel, given, _ = V.rows_kernel(
            e,
            (V.Operand("full", pitch=width),),
            rows,
            width,
            V.Operand("full", pitch=width),
            1,
        )
        trace = kernel.trace(given, machine=K.dsl_machine(dev.machine))
        kernels.append(dev.kernel(json.dumps(trace.graph)))
    variants = {}
    high = {"dram": dev.memory.dram.lo, "stage": dev.memory.staging[0].free_list[0][0]}
    for mode in ("off", "fused", "exported", "hostwrite", "layout"):
        x, h, y = [dev.alloc(rows * width * 2) for _ in range(3)]
        kernel = kernels[0]
        if mode == "layout":
            graph = dict(
                kernel.traced.graph,
                name="copy_identity",
                scratch=[],
                body=[
                    dict(
                        kind="mover",
                        op="copy",
                        src=dict(buffer="in0", axes=["all", "all"]),
                        dst=dict(buffer="out", axes=["all", "all"]),
                        flags=[],
                        kw=[],
                        where="native_copy:1",
                    )
                ],
            )
            kernel = dev.kernel(json.dumps(graph))
        with patch.dict(
            os.environ,
            {
                "KTPU_GRAPH_FUSE": "1",
                "KTPU_GRAPH_ELEMENTWISE": "0" if mode in ("off", "layout") else "1",
                "KTPU_GRAPH_LAYOUT": "1" if mode == "layout" else "0",
            },
        ):
            dev.record()
            dev.run(kernel, {"in0": x, "out": h})
            if mode == "hostwrite":
                dev.write(h, np.full((rows, width), 0.25, np.float16).tobytes())
            dev.run(kernels[1], {"in0": h, "out": y})
            if mode != "exported":
                dev.free(h)
            log = dev.recorded()
        variants[mode] = dict(
            segments=log.native.segments,
            before=log.native.before,
            after=log.native.after,
            rules=log.native.rules,
            build_seconds=log.native.build_seconds,
            x=dev.mmu.resolve(x)[1],
            y=dev.mmu.resolve(y)[1],
            nbytes=rows * width * 2,
        )
        high["dram"] = max(
            high["dram"], max(a + n for a, n in dev.memory.dram.live.items())
        )
        high["stage"] = max(
            high["stage"],
            max(
                (a + n for a, n in dev.memory.staging[0].live.items()),
                default=high["stage"],
            ),
        )
        log.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(dict(variants=variants, high=high)))
    print(
        json.dumps(
            {
                k: {n: v[n] for n in ("before", "after", "rules", "build_seconds")}
                for k, v in variants.items()
            },
            indent=2,
        )
    )
    dev.close()


def execute(args):
    artifact = pickle.loads(args.artifact.read_bytes())
    start = time.perf_counter()
    factory = functools.partial(VerilatorCard, fw=args.firmware, alarm=550)
    with patch.object(D, "VerilatorCard", factory):
        dev = D.Device(
            "sim",
            build=D.ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
            board="vlt_v9t6s_1n",
            log=args.output.with_suffix(".log"),
        )
    results = []
    try:
        dev.memory.alloc(artifact["high"]["dram"] - dev.memory.dram.lo)
        lo = dev.memory.staging[0].free_list[0][0]
        if artifact["high"]["stage"] > lo:
            dev.memory.stage(artifact["high"]["stage"] - lo, 0)
        with (
            patch.object(rs._rs, "compile", forbidden),
            patch.object(rs._rs, "library_prebuild", forbidden),
        ):
            for mode, saved in artifact["variants"].items():
                log = D.Recorded([], [], dev)
                log.native = NativeRecording(
                    dev,
                    saved["segments"],
                    [],
                    saved["before"],
                    saved["after"],
                    saved["rules"],
                    saved["build_seconds"],
                )
                for seed in (31, 57):
                    values = (
                        np.random.default_rng(seed).integers(
                            -40, 40, saved["nbytes"] // 2
                        )
                        / 32
                    ).astype(np.float16)
                    expected = np.maximum(
                        values
                        + (np.float16(0) if mode == "layout" else np.float16(0.125)),
                        np.float16(0),
                    ).astype(np.float16)
                    if mode == "hostwrite":
                        expected[:] = 0.25
                    dev.card.write(saved["x"], values.tobytes())
                    dev.card.write(saved["y"], bytes([0xA5]) * saved["nbytes"])
                    before = dev.card.queues[0].counters["submitted"]
                    run = dev.replay(log)[0]
                    actual = dev.card.get(saved["y"], saved["nbytes"])
                    assert actual == expected.tobytes(), (mode, seed)
                    results.append(
                        dict(
                            mode=mode,
                            seed=seed,
                            launches=saved["after"],
                            submissions=dev.card.queues[0].counters["submitted"]
                            - before,
                            cycles=run.cycles,
                            exact=True,
                        )
                    )
                log.close()
    finally:
        dev.close()
    args.output.write_text(
        json.dumps(
            dict(results=results, wall_seconds=time.perf_counter() - start), indent=2
        )
    )
    print(args.output.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("compile", "execute"))
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--firmware", type=Path)
    args = parser.parse_args()
    (compile_artifact(args.artifact) if args.mode == "compile" else execute(args))


if __name__ == "__main__":
    main()
