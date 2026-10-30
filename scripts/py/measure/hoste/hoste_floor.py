"""Measure the upstream eager floor by removing execution at the KTPU run seam."""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
import argparse
import cProfile
import json
import statistics
import time
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad.mnist_mlp import MLP
from hoste_direct_capture import capture_direct
from hseam import profile_rows, workload
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D
from tinygrad import Tensor, tensor
from tinygrad.helpers import Context


def no_execution(*args, **kwargs):
    return None


def creation_only(self, *args, **kwargs):
    return self


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workload", choices=("vit-eager", "lenet-eager", "mlp"))
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--warmup", type=int, default=4)
    ap.add_argument("--repeat", type=int, default=7)
    ap.add_argument(
        "--endpoint", choices=("none", "callback", "direct"), default="none"
    )
    args = ap.parse_args()
    with (
        D.attached(D.Device("offline", board="multimesh_v9t6", queue_depth=1)) as dev,
        (
            capture_direct(dev, callbacks=args.endpoint == "callback")
            if args.endpoint != "none"
            else nullcontext()
        ) as endpoint,
    ):
        if args.workload == "mlp":
            with Context(DEV="CPU"):
                model = MLP(128, 784)
            place(model)
            data = (
                Tensor(np.ones((1, 784), dtype=np.float16), device="KTPU").realize(),
            )
            execute = lambda x: model(x).realize()
        else:
            execute, arrays = workload(args.workload)
            data = tuple(Tensor(a, device="KTPU").realize() for a in arrays)
        result = {}
        launches = 0
        for mode in ("normal", "no_execution", "creation_only"):
            values = []
            saved = (
                [
                    (t, t.uop, t.grad)
                    for ref in list(tensor.all_tensors)
                    if (t := ref()) is not None
                ]
                if mode == "creation_only"
                else []
            )
            context = (
                nullcontext()
                if mode == "normal"
                else (
                    patch.object(Tensor, "realize", creation_only)
                    if mode == "creation_only"
                    else patch.object(tensor, "run_linear", no_execution)
                )
            )
            with context, Context(TRAINING=int(args.workload == "lenet-eager")):
                for index in range(args.warmup + args.repeat):
                    if mode == "creation_only":
                        for t, uop, grad in saved:
                            t.uop, t.grad = uop, grad
                    before = dev.counters["runs"]
                    start = time.process_time()
                    execute(*data)
                    dev.drain()
                    elapsed = time.process_time() - start
                    if mode == "normal":
                        launches = dev.counters["runs"] - before
                    if index >= args.warmup:
                        values.append(elapsed * 1000 / launches)
                if mode == "creation_only":
                    for t, uop, grad in saved:
                        t.uop, t.grad = uop, grad
                profile = cProfile.Profile()
                profile.enable()
                execute(*data)
                dev.drain()
                profile.disable()
                rows = profile_rows(profile)
                for row in rows:
                    row["exclusive_ms_per_launch"] = (
                        row["exclusive_seconds"] * 1000 / launches
                    )
                    row["inclusive_ms_per_launch"] = (
                        row["inclusive_seconds"] * 1000 / launches
                    )
                    row["calls_per_launch"] = row["calls"] / launches
            if mode == "creation_only":
                for t, uop, grad in saved:
                    t.uop, t.grad = uop, grad
            result[mode] = {
                "profile": rows,
                "endpoint": None if endpoint is None else dict(endpoint.queue.counters),
                "recipes": (
                    None
                    if dev.eager_recipes is None
                    else dict(dev.eager_recipes.counts)
                ),
                "captures": (
                    None if dev.eager_calls is None else dict(dev.eager_calls.counts)
                ),
                "samples_ms_per_launch": values,
                "median_ms_per_launch": statistics.median(values),
            }
        dev.close()
    result.update(
        workload=args.workload,
        launches=launches,
        numeric_execution=False,
        definition="no_execution removes all compiler/runtime/transport calls after Tensor.linear_with_vars; graph construction and tensor-to-function transformation still occur",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                k: (
                    {a: b for a, b in v.items() if a != "profile"}
                    if isinstance(v, dict)
                    else v
                )
                for k, v in result.items()
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
