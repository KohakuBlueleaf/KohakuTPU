"""Warm host dispatch attribution and raw per-package firmware idle records.

Run with the six workspace import roots on PYTHONPATH. Offline results describe
host work only; model time advances on transport commands, not host wall time.
"""

import argparse
import cProfile
import json
import os
import pstats
import runpy
import time
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from hseam_counting import CountingCard, CountingMemory
from kohakutpu.application.ktpugrad.device import KTPUGraph
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as D
from seam_timeline import HostTimeline
from tinygrad import Tensor, TinyJit
from tinygrad import schedule as tiny_schedule
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters

try:
    # The workstation's machine-wide model lock; card hosts do not install it.
    import ktpu_pytest_lock
except ModuleNotFoundError:
    ktpu_pytest_lock = None

ROOT = Path(__file__).resolve().parents[3]


class ScheduleCache(dict):
    """Observe tinygrad's existing cache without changing its equality keys."""

    def __init__(self, initial):
        super().__init__(initial)
        self.counts = dict.fromkeys(("hit", "miss", "read", "write"), 0)

    def __contains__(self, key):
        got = super().__contains__(key)
        self.counts["hit" if got else "miss"] += 1
        return got

    def get(self, key, default=None):
        self.counts["hit" if super().__contains__(key) else "miss"] += 1
        return super().get(key, default)

    def __getitem__(self, key):
        self.counts["read"] += 1
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        self.counts["write"] += 1
        return super().__setitem__(key, value)


def workload(name):
    """A seeded workload without downloads; model creation is outside timing."""
    Tensor.manual_seed(0)
    if name == "activation":

        def activation(x):
            for _ in range(4):
                x = x.silu().contiguous().realize()
            return x

        return activation, [np.linspace(-1, 1, 1024, dtype=np.float16)]
    if name.startswith("lenet"):
        example = runpy.run_path(
            str(ROOT / "software/application/examples/tinygrad/mnist_mlp.py")
        )
        with Context(DEV="CPU"):
            model = example["LeNet"]()
        place(model)
        opt = Adam16(get_parameters(model), lr=2e-3)

        def train(x, labels):
            return example["train_step"](model, opt, x, labels)

        run = TinyJit(train) if name == "lenet-jit" else train
        return run, [
            np.linspace(0, 1, 8 * 784, dtype=np.float16).reshape(8, 784),
            np.eye(10, dtype=np.float16)[np.arange(8) % 10],
        ]
    example = runpy.run_path(
        str(ROOT / "software/application/examples/tinygrad/vit_tiny.py")
    )
    model = example["seeded_model"](12, 17)
    place(model)
    return lambda x: model(x).realize(), [
        np.random.default_rng(18).standard_normal((1, 3, 224, 224)).astype(np.float16)
    ]


def profile_rows(profile):
    """Exclusive and inclusive times are separate, never summed as attribution."""
    rows = []
    for (filename, line, name), (primitive, calls, own, total, callers) in pstats.Stats(
        profile
    ).stats.items():
        filename = filename.replace("\\", "/")
        rows.append(
            {
                "file": filename,
                "line": line,
                "function": name,
                "primitive_calls": primitive,
                "calls": calls,
                "exclusive_seconds": own,
                "inclusive_seconds": total,
                "callers": [
                    {
                        "file": f.replace("\\", "/"),
                        "function": n,
                        "exclusive_seconds": v[2],
                    }
                    for (f, _, n), v in callers.items()
                ],
            }
        )
    return sorted(rows, key=lambda r: r["exclusive_seconds"], reverse=True)


def measure(args, result):
    graphs, events = [], []
    result.update(steps=[], events=events, profile_timer="perf_counter (elapsed)")
    start = time.perf_counter()
    with (
        D.attached(
            D.Device(
                args.device,
                board=args.board,
                nodes=1,
                build=args.build,
                queued=bool(args.queue),
                library=args.library,
                library_only=True,
                classes=bool(args.classes),
                queue_depth=args.depth,
            )
        ) as dev,
        ExitStack() as stack,
    ):
        schedule_cache = None
        if getattr(args, "schedule_cache", None) is not None:
            stack.enter_context(Context(SCACHE=args.schedule_cache))
            schedule_cache = ScheduleCache(tiny_schedule.schedule_cache)
            stack.enter_context(
                patch.object(tiny_schedule, "schedule_cache", schedule_cache)
            )
        if not args.counting and hasattr(dev.card, "queues"):
            counted = CountingMemory(dev.card.mem, dev.memory)
            counted.queues = dev.card.queues
            dev.card.mem = counted
            for q in dev.card.queues.values():
                q.mem = counted
        traffic_enabled = isinstance(getattr(dev.card, "mem", None), CountingMemory)
        result["device"] = repr(dev)
        result["depth"] = args.depth
        result["queue_bytes"] = dev.queue_bytes
        result["package_bytes_changed"] = False
        original_graph = KTPUGraph.__call__
        original_collect = dev._collect_one

        def graph_call(graph, *a, **kw):
            if graph not in graphs:
                graphs.append(graph)
            return original_graph(graph, *a, **kw)

        def budget():
            if time.perf_counter() - start > args.seconds:
                raise TimeoutError(f"measurement exceeded {args.seconds}s")

        def collect(pending):
            if pending.run is not None:
                return original_collect(pending)
            budget()
            before = time.perf_counter()
            got = original_collect(pending)
            event = {
                "step": len(result["steps"]),
                "name": pending.name,
                "plan": pending.info.get("plan"),
                "cycles": got.cycles,
                "host_collect_seconds": time.perf_counter() - before,
                "nodes": {},
            }
            raw = pending.result
            for n in pending.nodes:
                one = raw.get("nodes", {}).get(n, raw)
                event["nodes"][n] = {"phases": one.get("phases", {})}
            events.append(event)
            return got

        stack.enter_context(patch.object(KTPUGraph, "__call__", graph_call))
        stack.enter_context(patch.object(dev, "_collect_one", collect))
        if hasattr(dev.card, "_submit"):
            original_submit = dev.card._submit

            def submit(*a, **kw):
                budget()
                return original_submit(*a, **kw)

            stack.enter_context(patch.object(dev.card, "_submit", submit))
            dev.card.alarm = min(args.seconds, 60)
        timeline = None
        if getattr(args, "timeline", False):
            timeline = stack.enter_context(HostTimeline(dev))
            result["timeline"] = timeline.events
        try:
            run, arrays = workload(args.workload)
            for i in range(args.steps):
                budget()
                dev.drain()
                if timeline is not None:
                    timeline.start(i)
                inputs = [Tensor(a, device="KTPU").realize() for a in arrays]
                traffic = dev.card.mem.snapshot() if traffic_enabled else {}
                before = dict(dev.counters)
                cache_before = (
                    dict(schedule_cache.counts) if schedule_cache is not None else {}
                )
                calls_before = getattr(getattr(dev.card, "t", None), "calls", 0)
                transport_before = dict(
                    getattr(getattr(dev.card, "t", None), "counts", {})
                )
                queue_before = {
                    n: dict(q.counters)
                    for n, q in getattr(dev.card, "queues", {}).items()
                }
                profile = cProfile.Profile()
                t0 = time.perf_counter()
                cpu0 = time.process_time()
                if args.profile and i == args.steps - 1:
                    profile.enable()
                with Context(TRAINING=int(args.workload.startswith("lenet"))):
                    output = run(*inputs)
                dev.drain()
                if args.profile and i == args.steps - 1:
                    profile.disable()
                    result["profile"] = profile_rows(profile)
                cpu = time.process_time() - cpu0
                elapsed = time.perf_counter() - t0
                count = dev.counters["runs"] - before["runs"]
                row = {
                    "step": i,
                    "seconds": elapsed,
                    "host_cpu_seconds": cpu,
                    "host_cpu_ms_per_launch": cpu * 1000 / count if count else None,
                    "ms_per_launch": elapsed * 1000 / count if count else None,
                    "launches": count,
                    "profiled": bool(args.profile and i == args.steps - 1),
                    "schedule_cache": (
                        {
                            k: v - cache_before[k]
                            for k, v in schedule_cache.counts.items()
                        }
                        if schedule_cache is not None
                        else None
                    ),
                    "transport_counters": {
                        k: v - transport_before.get(k, 0)
                        for k, v in getattr(
                            getattr(dev.card, "t", None), "counts", {}
                        ).items()
                    },
                    "transport_calls": getattr(getattr(dev.card, "t", None), "calls", 0)
                    - calls_before,
                    "prepared_package_bytes": sum(
                        q.prepared_bytes
                        for q in getattr(dev.card, "queues", {}).values()
                    ),
                    "counters": {k: v - before[k] for k, v in dev.counters.items()},
                    "queues": {
                        n: {k: v - queue_before[n][k] for k, v in q.counters.items()}
                        for n, q in getattr(dev.card, "queues", {}).items()
                    },
                    "idle_cycles": (
                        sum(
                            one["phases"]["idle"]
                            for e in events
                            if e["step"] == i
                            for one in e["nodes"].values()
                        )
                        if dev.kind != "offline"
                        and all(
                            "idle" in one["phases"]
                            for e in events
                            if e["step"] == i
                            for one in e["nodes"].values()
                        )
                        else None
                    ),
                }
                if traffic_enabled:
                    row["backend_transport_calls"] = row["transport_calls"]
                    row["traffic"] = {
                        k: {
                            "calls": v[0] - traffic.get(k, [0, 0])[0],
                            "bytes": v[1] - traffic.get(k, [0, 0])[1],
                        }
                        for k, v in dev.card.mem.snapshot().items()
                    }
                    row["transport_calls"] = sum(
                        v["calls"] for v in row["traffic"].values()
                    )
                # Readback is not part of the host dispatch timing.
                if dev.kind != "offline":
                    values = output.numpy()
                    row["output"] = values.tolist()
                    row["output_hex"] = values.tobytes().hex()
                result["steps"].append(row)
                if timeline is not None:
                    timeline.active = False
                if not args.quiet:
                    print(
                        json.dumps(
                            {
                                k: v
                                for k, v in row.items()
                                if k not in ("output", "output_hex")
                            }
                        ),
                        flush=True,
                    )
            result["counters"] = dict(dev.counters)
            result["jit"] = [
                {
                    "counts": g.counts,
                    "misses": g.misses,
                    "log_events": (
                        {
                            op: sum(e[0] == op for e in g.log[1])
                            for op in ("run", "write", "read")
                        }
                        if g.log is not None
                        else None
                    ),
                }
                for g in graphs
            ]
            result["status"] = "measured"
        finally:
            result["wall_seconds"] = time.perf_counter() - start
            for graph in graphs:
                graph._release_logs()
            dev.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "workload", choices=("lenet-eager", "lenet-jit", "vit-eager", "activation")
    )
    parser.add_argument("--device", choices=D.KINDS, default="offline")
    parser.add_argument("--board", default="multimesh_v9t6")
    parser.add_argument("--build")
    parser.add_argument("--library", default="build/templates")
    parser.add_argument("--library-only", action="store_true", default=True)
    parser.add_argument("--classes", type=int, choices=(0, 1), default=0)
    parser.add_argument("--queue", type=int, choices=(0, 1), default=1)
    parser.add_argument("--head-cache", type=int, choices=(0, 1), default=1)
    parser.add_argument("--prepared", type=int, choices=(0, 1), default=0)
    parser.add_argument("--package-budget", type=int, default=16 << 20)
    parser.add_argument("--submit-batch", type=int, default=1)
    parser.add_argument("--cq-bulk", type=int, choices=(0, 1), default=0)
    parser.add_argument("--resident-bytes", type=int, default=0)
    parser.add_argument("--backoff", type=float, default=0)
    parser.add_argument("--depth", type=int)
    parser.add_argument("--steps", type=int, default=8)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--counting", action="store_true")
    parser.add_argument("--timeline", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--plan-cache", type=int, choices=(0, 1))
    parser.add_argument("--schedule-cache", type=int, choices=(0, 1))
    parser.add_argument("--admission-context", type=int, choices=(0, 1))
    parser.add_argument("--arena-lookup", type=int, choices=(0, 1))
    parser.add_argument("--host-submit-cache", type=int, choices=(0, 1))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.steps < 1 or (args.depth is not None and args.depth < 1):
        parser.error("steps and depth must be positive")
    for name, value in (
        ("KTPU_PLAN_CACHE", args.plan_cache),
        ("KOHAKUTPU_ADMISSION_CONTEXT", args.admission_context),
        ("KOHAKUTPU_ARENA_LOOKUP", args.arena_lookup),
        ("KOHAKUTPU_HOST_SUBMIT_CACHE", args.host_submit_cache),
    ):
        if value is not None:
            os.environ[name] = str(value)
    os.environ["KOHAKUTPU_SQ_HEAD_CACHE"] = str(args.head_cache)
    os.environ[D.PREPARED_REPLAY_ENV] = str(args.prepared)
    os.environ[D.REPLAY_PACKAGE_BYTES_ENV] = str(args.package_budget)
    os.environ["KOHAKUTPU_SUBMIT_BATCH"] = str(args.submit_batch)
    os.environ["KOHAKUTPU_CQ_BULK"] = str(args.cq_bulk)
    os.environ["KOHAKUTPU_PACKAGE_CACHE_BYTES"] = str(args.resident_bytes)
    os.environ["KOHAKUTPU_POLL_BACKOFF"] = str(args.backoff)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = {
        "arguments": {
            k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()
        }
    }
    locked = args.device == "sim"
    if locked and ktpu_pytest_lock is None:
        raise SystemExit("model runs need the ktpu_pytest_lock plugin")
    if locked:
        ktpu_pytest_lock.pytest_configure(SimpleNamespace())
    try:
        with ExitStack() as stack:
            if args.counting:
                if args.device != "offline":
                    parser.error("counting requires --device offline")
                stack.enter_context(patch.object(D, "OfflineCard", CountingCard))
            measure(args, result)
    except Exception as error:  # Preserve partial measurements.
        result.update(status="error", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if locked:
            ktpu_pytest_lock.pytest_unconfigure(SimpleNamespace())


if __name__ == "__main__":
    main()
