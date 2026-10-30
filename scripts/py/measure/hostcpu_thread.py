"""Bounded GIL experiment: pure planning beside counting queue submission/collection.

This double has zero device cycles. A concurrent worker measures host contention,
not the fraction of a real card's busy interval hidden by planning.
"""

import argparse
import inspect
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from hseam_counting import CountingCard
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad.lower import Access, Plan
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs
from tinygrad import Tensor, schedule
from tinygrad.dtype import dtypes
from tinygrad.uop.ops import AxisType, UOp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    machine = machine_of("multimesh_v9t6", nodes=1)
    kernel, given = V.flat_kernel(X.op("add", X.Load(0), 1.0), ("f16",), 128, "f16", 2)
    source = kernel.trace(given, machine=K.dsl_machine(machine)).graph_json
    library = str(args.output.parent / "thread-library")
    rs._rs.library_prebuild(library, source, machine.machine_json)
    r, c = UOp.range(32, 0, AxisType.LOOP), UOp.range(64, 1, AxisType.LOOP)
    plan = Plan(
        "vector",
        UOp.sink(),
        "thread",
        0,
        dtypes.half,
        (32, 64),
        (r, c),
        X.Load(0),
        [Access(1, dtypes.half, ((r, 64), (c, 1)), 0)],
    )
    result = {
        "iterations": args.iterations,
        "device_cycles": 0,
        "schedule_cache_symbols": [k for k in vars(schedule) if "cache" in k.lower()],
        "schedule_source": inspect.getsource(schedule),
        "schedule_cache_source": inspect.getsource(schedule.create_linear_with_vars),
        "linear_with_vars_source": inspect.getsource(Tensor.linear_with_vars),
        "trials": [],
    }
    with patch.object(D, "OfflineCard", CountingCard):
        dev = D.Device(
            "offline",
            board="multimesh_v9t6",
            library=library,
            library_only=True,
            classes=False,
        )
    x, y = dev.alloc(256), dev.alloc(256)
    compiled = dev.kernel(source)
    programs = compiled.programs(
        {"in0": dev.mmu.resolve(x)[1], "out": dev.mmu.resolve(y)[1]}
    )

    def planning():
        for _ in range(args.iterations):
            R._view.__wrapped__(plan)

    def submission():
        for _ in range(args.iterations):
            dev.card.collect(dev.card.submit(programs))

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            submission()  # warm bind, library and queue caches outside timing
            for threaded in (False, True, True, False):
                cpu, wall = time.process_time(), time.perf_counter()
                if threaded:
                    future = executor.submit(submission)
                    planning()
                    future.result()
                else:
                    planning()
                    submission()
                result["trials"].append(
                    {
                        "threaded": threaded,
                        "cpu_seconds": time.process_time() - cpu,
                        "wall_seconds": time.perf_counter() - wall,
                    }
                )
        result["compile_calls_in_measurement"] = dev.counters["compile_calls"]
    finally:
        dev.close()
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["trials"]))


if __name__ == "__main__":
    main()
