"""Bounded dispatch measurements: CPU phases, graph replay and firmware idle.

Offline mode compiles/checks packages but does not execute tensor arithmetic.
A sim invocation is one measurement session; --depth selects one queue limit.
"""

import argparse
import cProfile
import io
import json
import os
import pstats
import time
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad.mnist_mlp import MLP, LeNet
from examples.tinygrad.vit_tiny import seeded_model
from tinygrad import Tensor
from tinygrad.helpers import Context

from kohakutpu.application.ktpugrad import Inference
from kohakutpu.application.ktpugrad.device import KTPUGraph
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated, packages, rows


class Phases:
    """Inclusive CPU/wall phase totals; nested phases are not additive."""

    def __init__(self):
        self.totals = defaultdict(lambda: [0, 0.0, 0.0])

    def wrap(self, name, fn):
        def call(*args, **kwargs):
            cpu, wall = time.process_time(), time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                entry = self.totals[name]
                entry[0] += 1
                entry[1] += time.process_time() - cpu
                entry[2] += time.perf_counter() - wall

        return call


def workload(name, blocks, image_size, width, features, hidden):
    Tensor.manual_seed(17)
    with Context(DEV="CPU"):
        if name == "vit":
            model = seeded_model(blocks, 17, width, image_size)
            shape = (1, 3, image_size, image_size)
        else:
            model = LeNet() if name == "lenet" else MLP(hidden, features)
            shape = (1, 784 if name == "lenet" else features)
    return model, shape


def measure(args):
    model, shape = workload(
        args.model, args.blocks, args.image_size, args.width, args.features, args.hidden
    )
    dev = D.Device(
        args.device,
        board=args.board,
        nodes=1,
        queue_depth=args.depth,
        library=args.library,
        library_only=args.library_only,
        transport=args.transport,
    )
    rng = np.random.default_rng(18)
    phases, graphs, records = Phases(), [], []
    original_graph = KTPUGraph.__call__

    def graph(self, *a, **kw):
        if self not in graphs:
            graphs.append(self)
        return original_graph(self, *a, **kw)

    original_result = getattr(dev.card, "_result", None)

    def result(*a, **kw):
        got = original_result(*a, **kw)
        records.append(dict(got["phases"], cycles=got["cycles"]))
        return got

    def forward(x):
        return model(x).realize()

    run = Inference(forward, replay=args.replay)
    samples = []
    profile = cProfile.Profile()
    try:
        with D.attached(dev), ExitStack() as stack:
            place(model)
            stack.enter_context(patch.object(KTPUGraph, "__call__", graph))
            if original_result is not None:
                stack.enter_context(patch.object(dev.card, "_result", result))
            for index in range(args.warmup + args.repeat):
                x = Tensor(
                    rng.standard_normal(shape).astype(np.float16), device="KTPU"
                ).realize()
                start_runs, start_records = len(dev.runs), len(records)
                before = dict(dev.counters)
                traffic_before = (
                    dev.card.mem.snapshot() if hasattr(dev.card.mem, "snapshot") else {}
                )
                queue_before = {
                    n: dict(q.counters)
                    for n, q in getattr(dev.card, "queues", {}).items()
                }
                with ExitStack() as timed:
                    if index == args.warmup:
                        for obj, attr, name in (
                            (generated, "launch", "generate_launch"),
                            (rows, "run", "rows"),
                            (D.Device, "submit", "submit"),
                            (D.Device, "_collect_one", "collect"),
                            (packages, "built", "bind_admit"),
                        ):
                            timed.enter_context(
                                patch.object(
                                    obj, attr, phases.wrap(name, getattr(obj, attr))
                                )
                            )
                        profile.enable()
                    cpu, wall = time.process_time(), time.perf_counter()
                    y = run(x)
                    if args.device != "offline":
                        output = y.numpy().astype(np.float32).tolist()
                    else:
                        output = None
                    dev.drain()
                    elapsed_cpu, elapsed_wall = (
                        time.process_time() - cpu,
                        time.perf_counter() - wall,
                    )
                    if index == args.warmup:
                        profile.disable()
                current = records[start_records:]
                sample = dict(
                    index=index,
                    launches=len(dev.runs) - start_runs,
                    cpu_seconds=elapsed_cpu,
                    wall_seconds=elapsed_wall,
                    idle_cycles=(
                        sum(r.get("idle", 0) for r in current) if current else None
                    ),
                    busy_cycles=sum(r["cycles"] for r in current) if current else None,
                    firmware_phases={
                        k: sum(r.get(k, 0) for r in current)
                        for k in ("head", "bind", "steps", "barrier")
                    },
                    counters={k: v - before[k] for k, v in dev.counters.items()},
                    traffic=(
                        {
                            k: [
                                v[i] - traffic_before.get(k, [0, 0])[i]
                                for i in range(2)
                            ]
                            for k, v in dev.card.mem.snapshot().items()
                        }
                        if hasattr(dev.card.mem, "snapshot")
                        else {}
                    ),
                    queues={
                        n: {k: v - queue_before[n][k] for k, v in q.counters.items()}
                        for n, q in getattr(dev.card, "queues", {}).items()
                    },
                )
                samples.append(sample)
                if not args.quiet:
                    print(json.dumps(dict(sample=sample)), flush=True)
            stream = io.StringIO()
            pstats.Stats(profile, stream=stream).sort_stats("tottime").print_stats(25)
            if not args.quiet:
                print(stream.getvalue(), flush=True)
            report = dict(
                configuration=dict(vars(args)),
                samples=samples,
                records=(
                    [
                        dict(name=r.name, kind=r.kind, **p)
                        for r, p in zip(dev.runs, records, strict=True)
                    ]
                    if original_result is not None
                    else []
                ),
                phases=dict(phases.totals),
                profile=stream.getvalue(),
                graphs=[dict(counts=g.counts, misses=g.misses[-3:]) for g in graphs],
                output=output,
                numeric_execution=args.device != "offline",
                environment={
                    k: v
                    for k, v in os.environ.items()
                    if k.startswith(("KTPU_", "KOHAKUTPU_"))
                },
            )
            warm = samples[args.warmup + 1 :]
            report["summary"] = dict(
                launches=warm[-1]["launches"],
                cpu_ms_per_launch=float(
                    np.median([s["cpu_seconds"] * 1000 / s["launches"] for s in warm])
                ),
                wall_seconds=float(np.median([s["wall_seconds"] for s in warm])),
                idle_cycles=warm[-1]["idle_cycles"],
                busy_cycles=warm[-1]["busy_cycles"],
                queues=warm[-1]["queues"],
                traffic=warm[-1]["traffic"],
            )
            if args.output:
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(
                    json.dumps(report, indent=2) + "\n", encoding="utf-8"
                )
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in report.items()
                        if k not in ("samples", "profile", "output", "records")
                        and (not args.quiet or k in ("configuration", "summary"))
                    }
                ),
                flush=True,
            )
            return report
    finally:
        graphs.clear()
        dev.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=("vit", "lenet", "mlp"))
    parser.add_argument(
        "--device", choices=("offline", "sim", "card"), default="offline"
    )
    parser.add_argument("--transport", choices=D.TRANSPORTS)
    parser.add_argument("--board", default="multimesh_v9t6")
    parser.add_argument("--depth", type=int, default=1)
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--warmup", type=int, default=6)
    parser.add_argument("--repeat", "--inputs", type=int, default=3)
    parser.add_argument("--blocks", type=int, default=12)
    parser.add_argument("--image-size", type=int, default=224)
    parser.add_argument("--width", type=int, default=192)
    parser.add_argument("--features", type=int, default=784)
    parser.add_argument("--hidden", type=int, default=128)
    parser.add_argument("--library")
    parser.add_argument("--library-only", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--knobs",
        action="store_true",
        help="individual planning, residency, publication and prepared knobs",
    )
    parser.add_argument("--depth-sweep", action="store_true")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="eager caches off/on then inference replay, sharing the process compile cache",
    )
    args = parser.parse_args()
    if args.repeat < 2:
        parser.error(
            "repeat/inputs must be at least two: one profile sample and steady samples"
        )
    base = args.output
    for depth in (1, 2, 4, 8) if args.depth_sweep else (args.depth,):
        args.depth = depth
        reports = []
        variants = (
            [("before", "0", False), ("after", "1", False), ("replay", "1", True)]
            if args.compare
            else [("selected", None, args.replay)]
        )
        if args.knobs:
            variants = [
                ("before", "0", False),
                *[
                    (k, "0", False)
                    for k in (
                        "plan",
                        "specialise",
                        "resident",
                        "head",
                        "batch",
                        "flush",
                    )
                ],
                ("unprepared", "1", True),
                ("replay", "1", True),
            ]
        for label, enabled, replay in variants:
            for key in (
                "KTPU_PLAN_CACHE",
                "KTPU_SPECIALISE_CACHE",
                "KOHAKUTPU_EAGER_FLUSH",
                "KOHAKUTPU_SQ_HEAD_CACHE",
                "KOHAKUTPU_PREPARED_REPLAY",
            ):
                if enabled is not None:
                    os.environ[key] = enabled
            if enabled is not None:
                os.environ["KOHAKUTPU_PACKAGE_CACHE_BYTES"] = (
                    str(16 << 20) if enabled == "1" else "0"
                )
            if enabled is not None:
                os.environ["KOHAKUTPU_SUBMIT_BATCH"] = (
                    str(min(depth, 4)) if enabled == "1" else "1"
                )
            changes = {
                "plan": {"KTPU_PLAN_CACHE": "1"},
                "specialise": {"KTPU_SPECIALISE_CACHE": "1"},
                "resident": {"KOHAKUTPU_PACKAGE_CACHE_BYTES": str(16 << 20)},
                "head": {"KOHAKUTPU_SQ_HEAD_CACHE": "1"},
                "batch": {"KOHAKUTPU_SUBMIT_BATCH": str(min(depth, 4))},
                "flush": {
                    "KOHAKUTPU_SUBMIT_BATCH": str(min(depth, 4)),
                    "KOHAKUTPU_EAGER_FLUSH": "1",
                },
                "unprepared": {"KOHAKUTPU_PREPARED_REPLAY": "0"},
            }
            os.environ.update(changes.get(label, {}))
            args.replay = replay
            args.output = f"{base}-d{depth}-{label}.json" if base else None
            reports.append(measure(args))
        if args.compare and args.device != "offline":
            expected = np.asarray(reports[0]["output"])
            print(
                json.dumps(
                    dict(
                        depth=depth,
                        max_absolute_parity_errors=[
                            float(np.max(np.abs(np.asarray(r["output"]) - expected)))
                            for r in reports[1:]
                        ],
                    )
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
