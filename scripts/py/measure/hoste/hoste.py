"""HOSTE warm eager CPU layers; counting mode has no tensor arithmetic."""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
import argparse
import cProfile
import json
import os
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad.mnist_mlp import MLP
from hseam import ScheduleCache, profile_rows, workload
from hseam_counting import CountingCard
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D
from tinygrad import Tensor, schedule, tensor
from tinygrad.helpers import Context


def layer(file, function):
    if "kohakutpu.compiler._rs" in function or "LaunchRecipe" in function:
        return "Rust"
    if "/tinygrad/" in file:
        return "tinygrad"
    if "/driver/" in file or "hseam_counting.py" in file:
        return "transport"
    if "/kohakutpu/" in file or "/kohakuaccel/" in file:
        return "plan/runtime"
    return "other"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workload", choices=("vit-eager", "lenet-eager", "mlp"))
    ap.add_argument("--native", type=int, choices=(0, 1), default=0)
    ap.add_argument("--schedule", type=int, choices=(0, 1), default=0)
    ap.add_argument("--recipe", type=int, choices=(0, 1), default=0)
    ap.add_argument("--wire", type=int, choices=(0, 1), default=0)
    ap.add_argument("--backend", choices=("counting", "offline"), default="counting")
    ap.add_argument("--steps", type=int, default=5)
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    os.environ["KTPU_NATIVE_RECIPE"] = str(args.native)
    os.environ["KTPU_EAGER_SCHEDULE"] = str(args.schedule)
    os.environ["KTPU_EAGER_RECIPE"] = str(args.recipe)
    os.environ["KTPU_WIRE_RECIPE"] = str(args.wire)
    original = tensor.create_linear_with_vars
    shapes = Counter()
    identities = {}
    reuse = Counter()

    def observe(sink):
        key = sink.src[0] if sink.op.name == "CALL" else sink
        reuse["hit" if key in identities else "miss"] += 1
        identities[key] = True
        linear, values = original(sink)
        shapes[
            str(
                (
                    sink.op.name,
                    tuple(sorted({s.op.name for s in sink.src[1:]})),
                    tuple(sorted({c.op.name for c in linear.src})),
                    tuple(sorted({b.op.name for c in linear.src for b in c.src[1:]})),
                )
            )
        ] += 1
        return linear, values

    cache = ScheduleCache(schedule.schedule_cache)
    samples = []
    with (
        patch.object(
            D,
            "OfflineCard",
            CountingCard if args.backend == "counting" else D.OfflineCard,
        ),
        patch.object(tensor, "create_linear_with_vars", observe),
        patch.object(schedule, "schedule_cache", cache),
        Context(SCACHE=1),
        D.attached(
            D.Device("offline", board="multimesh_v9t6", queue_depth=args.depth)
        ) as dev,
    ):
        if args.workload == "mlp":
            Tensor.manual_seed(0)
            with Context(DEV="CPU"):
                model = MLP(128, 784)
            place(model)
            run = lambda x: model(x).realize()
            arrays = [np.linspace(0, 1, 784, dtype=np.float16).reshape(1, 784)]
        else:
            run, arrays = workload(args.workload)
        for i in range(args.steps):
            inputs = [Tensor(a, device="KTPU").realize() for a in arrays]
            before = dev.counters["runs"]
            counts = dict(cache.counts)
            seen = dict(reuse)
            prof = cProfile.Profile()
            cpu, wall = time.process_time(), time.perf_counter()
            if i == args.steps - 2:
                prof.enable()
            with Context(TRAINING=int(args.workload == "lenet-eager")):
                run(*inputs)
            dev.drain()
            prof.disable()
            sample = {
                "index": i,
                "cpu": time.process_time() - cpu,
                "wall": time.perf_counter() - wall,
                "launches": dev.counters["runs"] - before,
                "profiled": i == args.steps - 2,
                "scache": {k: v - counts[k] for k, v in cache.counts.items()},
                "identity": {k: v - seen.get(k, 0) for k, v in reuse.items()},
            }
            samples.append(sample)
            if sample["profiled"]:
                profile = profile_rows(prof)
            print(json.dumps(sample), flush=True)
        native = [r for c, _ in dev.kernels.values() for r in c._recipes.values()]
        recipes = {
            "count": len(native),
            "invariant": sum(r.admission_invariant for r in native),
            "wire": sum(r.wire_ready for r in native),
        }
        schedules = (
            {
                "hits": dev.eager_schedules.hits,
                "misses": dev.eager_schedules.misses,
                "kept": len(dev.eager_schedules.entries),
            }
            if dev.eager_schedules is not None
            else None
        )
        eager = (
            dict(dev.eager_recipes.counts) if dev.eager_recipes is not None else None
        )
        sequences = (
            None if dev.eager_sequences is None else dict(dev.eager_sequences.counts)
        )
        queue_counts = {n: dict(q.counters) for n, q in dev.card.queues.items()}
        dev.close()
    layers = defaultdict(float)
    launches = samples[-2]["launches"]
    for row in profile:
        name = layer(row["file"], row["function"])
        if row["file"] == "~" and name == "other" and row["callers"]:
            for caller in row["callers"]:
                layers[layer(caller["file"], caller["function"])] += (
                    caller["exclusive_seconds"] * 1000 / launches
                )
        else:
            layers[name] += row["exclusive_seconds"] * 1000 / launches
    warm = [s for s in samples[2:] if not s["profiled"]]
    result = {
        "configuration": vars(args) | {"output": str(args.output)},
        "numeric_execution": False,
        "samples": samples,
        "warm_cpu_ms_per_launch": statistics.median(
            s["cpu"] * 1000 / s["launches"] for s in warm
        ),
        "profile_elapsed_ms_per_launch": dict(layers),
        "sequences": sequences,
        "queues": queue_counts,
        "recipes": recipes,
        "eager": eager,
        "schedules": schedules,
        "schedule_shapes": dict(shapes),
        "profile": profile,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k not in ("profile", "samples")}
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
