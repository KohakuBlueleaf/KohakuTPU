"""Whole-workload native Recorded lifecycle, offline artifact and model parity."""

import argparse
import functools
import json
import pickle
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.graph.native_measure import forbidden
from examples.tinygrad.mnist_mlp import LeNet, train_step
from examples.tinygrad.vit_tiny import Block, seeded_model
from tinygrad import Tensor
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters, get_state_dict

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.card import VerilatorCard
from kohakutpu.application.runtime.graph.recorded import NativeRecording, prepare
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def build(args):
    start = time.perf_counter()
    dev = D.Device("offline", board="vlt_v9t6s_1n")
    rng = np.random.default_rng(71)
    cases = []
    with D.attached(dev):
        if args.workload == "math":
            x = X.Load(0)
            z = X.op(
                "mul",
                -2.302208198144325,
                X.op(
                    "add", x, X.op("mul", 0.044715, X.op("mul", X.op("mul", x, x), x))
                ),
            )
            gelu = X.op("mul", x, X.op("recip", X.op("add", 1, X.op("exp2", z))))
            for name, expression, values in (
                (
                    "exp2",
                    X.op("exp2", x),
                    [
                        -512,
                        -256,
                        -255,
                        -200,
                        -150,
                        -129,
                        -128,
                        -127,
                        -126,
                        -16,
                        -1,
                        0,
                        1,
                        8,
                        15,
                        16,
                        127,
                        128,
                        129,
                        150,
                        200,
                        255,
                        256,
                        512,
                        float("inf"),
                        float("-inf"),
                        -2,
                        2,
                        3,
                        4,
                        5,
                        6,
                    ],
                ),
                (
                    "gelu",
                    gelu,
                    [
                        -64,
                        -32,
                        -24,
                        -16,
                        -13,
                        -12.046875,
                        -12,
                        -11,
                        -10,
                        -9,
                        -8,
                        -7,
                        -6,
                        -5,
                        -4,
                        -3,
                        -2,
                        -1,
                        -0.5,
                        0,
                        0.5,
                        1,
                        2,
                        3,
                        4,
                        6,
                        8,
                        10,
                        12,
                        16,
                        32,
                        64,
                    ],
                ),
            ):
                v = np.tile(np.array(values, np.float16), (4, 1))
                f = v.astype(np.float64)
                with np.errstate(over="ignore", invalid="ignore"):
                    result = (
                        np.exp2(f)
                        if name == "exp2"
                        else 0.5
                        * f
                        * (1 + np.tanh(np.sqrt(2 / np.pi) * (f + 0.044715 * f**3)))
                    ).astype(np.float32)
                    # Hardware F16 pack saturates finite F32 overflows, but retains
                    # actual F32 infinities. This is distinct from NumPy cast overflow.
                    expected = np.where(
                        np.isfinite(result), np.clip(result, -65504, 65504), result
                    ).astype(np.float16)
                kernel, given, _ = V.rows_kernel(
                    expression,
                    (V.Operand("full", pitch=32),),
                    4,
                    32,
                    V.Operand("full", pitch=32),
                    1,
                )
                source = json.dumps(
                    kernel.trace(given, machine=K.dsl_machine(dev.machine)).graph
                )
                compiled = dev.kernel(source)
                a, b = dev.alloc(v.nbytes), dev.alloc(v.nbytes)
                dev.write(a, v.tobytes())
                dev.record()
                dev.run(compiled, {"in0": a, "out": b})
                log = dev.recorded()
                cases.append(
                    (
                        name,
                        log,
                        [(dev.mmu.resolve(b)[1], v.nbytes, "<f2", expected.tobytes())],
                    )
                )
        else:
            with Context(DEV="CPU"):
                if args.workload == "vit":
                    model = seeded_model(args.blocks, 71, args.width, args.image)
                else:
                    model = Block(args.width) if args.workload == "block" else LeNet()
                    for name, t in get_state_dict(model).items():
                        data = (
                            np.ones(t.shape)
                            if "norm" in name and name.endswith("weight")
                            else rng.standard_normal(t.shape) * 0.02
                        )
                        t.replace(Tensor(data.astype(np.float16), device="CPU"))
            place(model)
            shape = (
                (args.batch, 3, args.image, args.image)
                if args.workload == "vit"
                else (
                    (args.batch, args.tokens, args.width)
                    if args.workload == "block"
                    else (args.batch, 784)
                )
            )
            x = Tensor(
                rng.standard_normal(shape).astype(np.float16), device="KTPU"
            ).realize()
            if args.workload == "lenet":
                labels = Tensor(
                    np.eye(10, dtype=np.float16)[rng.integers(0, 10, args.batch)],
                    device="KTPU",
                ).realize()
                optimizer = Adam16(get_parameters(model), lr=2e-3)
            dev.record()
            if args.workload == "lenet":
                with Context(TRAINING=1):
                    loss, logits = train_step(model, optimizer, x, labels)
                outputs = [loss, logits, *get_parameters(model)]
            else:
                outputs = [model(x).realize()]
            dev.drain()
            log = dev.recorded()
            # Discover each already-computed tensor's readback spans outside capture.
            observed = []
            for value in outputs:
                dev.card.record()
                dtype = value.numpy().dtype.str
                events = dev.card.recorded()
                reads = [event for event in events if event[0] == "read"]
                assert len(reads) == 1, reads
                observed.append((*reads[0][1:], dtype, None))
            cases.append((args.workload, log, observed))
        # Preserve seeded physical inputs, including deferred host writes. Writes in
        # the recording still execute in their original dependency order.
        initial = [
            (page << 20, bytes(data)) for page, data in dev.card.mem.pages.items()
        ]
        variants = []
        for name, log, observed in cases:
            for mode in ("off", "all"):
                native = prepare(
                    log,
                    quant=mode == "all",
                    elementwise=mode == "all",
                    norm=mode == "all",
                    layout=mode == "all",
                    local=mode == "all",
                )
                variants.append(
                    dict(
                        name=name,
                        mode=mode,
                        segments=native.segments,
                        before=native.before,
                        after=native.after,
                        rules=native.rules,
                        build_seconds=native.build_seconds,
                        observed=observed,
                    )
                )
                # Hold both native allocations through export; never let one variant
                # alias a still-live captured input or output.
        high = dict(
            dram=max(at + n for at, n in dev.memory.dram.live.items()),
            stage=max(
                (at + n for at, n in dev.memory.staging[0].live.items()), default=0
            ),
        )
        artifact = dict(
            variants=variants,
            high=high,
            initial=initial,
            compile_seconds=time.perf_counter() - start,
            workload=args.workload,
            width=args.width,
            tokens=(
                (args.image // 16) ** 2 + 1 if args.workload == "vit" else args.tokens
            ),
            batch=args.batch,
            blocks=args.blocks,
            image=args.image,
        )
        args.artifact.parent.mkdir(parents=True, exist_ok=True)
        args.artifact.write_bytes(pickle.dumps(artifact))
        print(
            json.dumps(
                {k: v for k, v in artifact.items() if k not in ("initial", "variants")},
                indent=2,
            )
        )
        print(
            json.dumps(
                [
                    {
                        k: v
                        for k, v in variant.items()
                        if k not in ("segments", "observed")
                    }
                    for variant in variants
                ],
                indent=2,
            )
        )
    dev.close()


def execute(args):
    artifact = pickle.loads(args.artifact.read_bytes())
    factory = functools.partial(VerilatorCard, fw=args.firmware, alarm=550)
    start = time.perf_counter()
    with patch.object(D, "VerilatorCard", factory):
        dev = D.Device(
            "sim",
            build=D.ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
            board="vlt_v9t6s_1n",
            log=args.output.with_suffix(".log"),
        )
    references = (
        {}
        if args.reference is None
        else json.loads(args.reference.read_text())["references"]
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
            for saved in artifact["variants"]:
                if saved["mode"] not in args.modes.split(","):
                    continue
                for at, data in artifact["initial"]:
                    dev.card.write(at, data)
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
                before = dev.card.queues[0].counters["submitted"]
                run = dev.replay(log, wait=False)[0]
                actual = [
                    dev.card.get(at, size) for at, size, _, _ in saved["observed"]
                ]
                error = []
                for raw, (_, _, dtype, expected) in zip(
                    actual, saved["observed"], strict=True
                ):
                    values = np.frombuffer(raw, np.dtype(dtype))
                    if expected is not None:
                        target = np.frombuffer(expected, np.dtype(dtype))
                        np.testing.assert_allclose(
                            values, target, rtol=0.004, atol=0.002
                        )
                        finite = np.isfinite(target)
                        error.append(
                            float(
                                np.max(
                                    np.abs(
                                        values[finite].astype(np.float32)
                                        - target[finite]
                                    )
                                )
                            )
                        )
                    else:
                        assert np.isfinite(values).all(), saved["name"]
                output = [raw.hex() for raw in actual]
                compared = saved["name"] in references
                if not compared:
                    assert saved["mode"] == "off"
                    references[saved["name"]] = output
                else:
                    assert references[saved["name"]] == output, (
                        saved["name"],
                        "fusion parity",
                    )
                results.append(
                    dict(
                        name=saved["name"],
                        mode=saved["mode"],
                        launches=saved["after"],
                        cycles=run.cycles,
                        submissions=dev.card.queues[0].counters["submitted"] - before,
                        compared=compared,
                        independent_max_abs=error,
                    )
                )
                log.close()
                args.output.write_text(
                    json.dumps(
                        dict(
                            results=results,
                            references=references,
                            wall_seconds=time.perf_counter() - start,
                        ),
                        indent=2,
                    )
                )
                print(json.dumps(results[-1]), flush=True)
    finally:
        dev.close()
    args.output.write_text(
        json.dumps(
            dict(
                results=results,
                references=references,
                wall_seconds=time.perf_counter() - start,
            ),
            indent=2,
        )
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("compile", "execute"))
    p.add_argument(
        "--workload", choices=("math", "block", "vit", "lenet"), default="block"
    )
    p.add_argument("--artifact", type=Path, required=True)
    p.add_argument("--output", type=Path)
    p.add_argument("--firmware", type=Path)
    p.add_argument("--reference", type=Path)
    p.add_argument("--modes", default="off,all")
    p.add_argument("--batch", type=int, default=1)
    p.add_argument("--width", type=int, default=192)
    p.add_argument("--tokens", type=int, default=197)
    p.add_argument("--image", type=int, default=224)
    p.add_argument("--blocks", type=int, default=12)
    args = p.parse_args()
    (build if args.mode == "compile" else execute)(args)


if __name__ == "__main__":
    main()
