"""Offline wire parity and plan-reference schedule parity, not FPGA arithmetic."""

import argparse
import gzip
import json
import os
import pickle
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad.mnist_mlp import MLP, LeNet, train_step
from examples.tinygrad.vit_tiny import seeded_model
from hoste_direct_capture import capture_direct
from kohakutpu.application.ktpugrad import reference, run
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as D
from tinygrad import Tensor
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters


class ReferenceExecution:
    def __init__(self):
        self.calls = 0

    def __call__(self, dev, plan, buffers):
        self.calls += 1
        return reference.execute(dev, plan, buffers)


def capture(args):
    os.environ["KTPU_NATIVE_RECIPE"] = str(args.mode)
    os.environ["KTPU_EAGER_SCHEDULE"] = str(args.mode)
    numeric = args.kind == "reference-schedule"
    os.environ["KTPU_EAGER_RECIPE"] = str(args.mode if args.recipe else 0)
    os.environ["KTPU_WIRE_RECIPE"] = str(args.mode if args.wire else 0)
    observer = ReferenceExecution()
    values = {}
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = (
            LeNet()
            if args.model == "lenet"
            else (
                MLP(args.width, 784)
                if args.model == "mlp"
                else seeded_model(
                    args.blocks, 17, width=args.width, image_size=args.image_size
                )
            )
        )
    with (
        D.attached(D.Device("offline", board=args.board, queue_depth=1)) as dev,
        patch.object(run, "execute", observer) if numeric else nullcontext(),
        capture_direct(dev) if args.direct else nullcontext() as direct,
    ):
        checkpoints = {}
        if args.execution:
            dev.card.record()
        place(model)
        opt = Adam16(get_parameters(model), lr=2e-3) if args.model == "lenet" else None
        rng = np.random.default_rng(18)
        shape = (
            (args.batch, 784)
            if args.model in ("lenet", "mlp")
            else (1, 3, args.image_size, args.image_size)
        )
        array = rng.standard_normal(shape).astype(np.float16)
        if not args.execution:
            dev.card.record()
        for i in range(args.steps):
            if args.vary_inputs:
                array = rng.standard_normal(shape).astype(np.float16)
            x = Tensor(array, device="KTPU").realize()
            with Context(TRAINING=int(opt is not None)):
                if opt is None:
                    output = model(x)
                    if args.diverge:
                        output = (output + 1) if i % 2 else (output * 2)
                    result = (output.realize(),)
                else:
                    y = Tensor(
                        np.eye(10, dtype=np.float16)[np.arange(args.batch) % 10],
                        device="KTPU",
                    ).realize()
                    result = train_step(model, opt, x, y)
            dev.drain()
            if args.execution:
                tensors = {f"step{i}-out{j}": t for j, t in enumerate(result)}
                if opt is not None:
                    tensors.update(
                        {
                            f"step{i}-param{j}": p
                            for j, p in enumerate(get_parameters(model))
                        }
                    )
                for name, tensor in tensors.items():
                    start = len(dev.card.log)
                    value = tensor.numpy()
                    checkpoints[name] = {
                        "start": start,
                        "end": len(dev.card.log),
                        "shape": list(value.shape),
                        "dtype": str(value.dtype),
                    }
            if numeric:
                for j, tensor in enumerate(result):
                    values[f"step{i}-out{j}"] = tensor.numpy()
                if opt is not None:
                    values.update(
                        {
                            f"step{i}-param{j}": p.numpy()
                            for j, p in enumerate(get_parameters(model))
                        }
                    )
        events = dev.card.recorded()
        summary = {
            "mode": args.mode,
            "model": args.model,
            "kind": args.kind,
            "launches": dev.counters["runs"],
            "events": len(events),
            "reference_calls": observer.calls,
            "eager": (
                None if dev.eager_recipes is None else dict(dev.eager_recipes.counts)
            ),
            "schedules": (
                None
                if dev.eager_schedules is None
                else {
                    "hits": dev.eager_schedules.hits,
                    "misses": dev.eager_schedules.misses,
                }
            ),
            "native_direct_calls": (
                0 if direct is None else direct.queue.counters["submitted"]
            ),
            "accelerator_numeric_execution": False,
            "high_water": dev.dram.high_water,
            "arena_start": dev.dram.lo,
            "staging": {
                n: (arena.lo, arena.high_water)
                for n, arena in dev.memory.staging.items()
            },
        }
        dev.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.execution:
        with gzip.open(args.output, "wb") as output:
            pickle.dump(
                {
                    "events": events,
                    "checkpoints": checkpoints,
                    "configuration": vars(args) | {"output": str(args.output)},
                    "summary": summary,
                },
                output,
            )
    elif numeric:
        np.savez(args.output, **values)
    else:
        with args.output.open("wb") as output:
            pickle.dump(events, output)
    print(json.dumps(summary), flush=True)


def compare(args):
    if args.kind == "wire":
        with args.left.open("rb") as source:
            left = pickle.load(source)  # Trusted local measurement transcripts only.
        with args.right.open("rb") as source:
            right = pickle.load(source)
        assert len(left) == len(right), (len(left), len(right))
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            assert a == b, f"wire event {index} differs ({a[0]}, {b[0]})"
        summary = {
            "kind": args.kind,
            "byte_identical": True,
            "events": len(left),
            "accelerator_numeric_execution": False,
        }
    else:
        with np.load(args.left) as left, np.load(args.right) as right:
            assert left.files == right.files
            for key in left.files:
                a, b = left[key], right[key]
                assert (
                    a.dtype == b.dtype
                    and a.shape == b.shape
                    and a.tobytes() == b.tobytes()
                ), key
            summary = {
                "kind": args.kind,
                "bit_identical": True,
                "arrays": len(left.files),
                "elements": sum(left[k].size for k in left.files),
                "nonfinite": sum(
                    int((~np.isfinite(left[k])).sum()) for k in left.files
                ),
                "accelerator_numeric_execution": False,
            }
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True)
    cap = commands.add_parser("capture")
    cap.add_argument("model", choices=("lenet", "vit", "mlp"))
    cap.add_argument("--mode", type=int, choices=(0, 1), required=True)
    cap.add_argument("--steps", type=int, default=3)
    cap.add_argument("--recipe", action="store_true")
    cap.add_argument("--wire", action="store_true")
    cap.add_argument("--execution", action="store_true")
    cap.add_argument("--direct", action="store_true")
    cap.add_argument("--vary-inputs", action="store_true")
    cap.add_argument("--diverge", action="store_true")
    cap.add_argument("--board", default="multimesh_v9t6")
    cap.add_argument("--batch", type=int, default=8)
    cap.add_argument("--blocks", type=int, default=12)
    cap.add_argument("--width", type=int, default=192)
    cap.add_argument("--image-size", type=int, default=224)
    diff = commands.add_parser("compare")
    diff.add_argument("left", type=Path)
    diff.add_argument("right", type=Path)
    for p in (cap, diff):
        p.add_argument("--kind", choices=("wire", "reference-schedule"), default="wire")
        p.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    (capture if args.command == "capture" else compare)(args)


if __name__ == "__main__":
    main()
