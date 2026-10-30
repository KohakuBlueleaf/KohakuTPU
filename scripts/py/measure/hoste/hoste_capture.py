"""Interleaved old/native concrete-call transformation on the same observed graphs.

Cold templates are built outside timing. The harness retains observed graphs
only for this measurement; runtime capture stores address-parametric templates.
"""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
import argparse
import json
import os
import statistics
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad.mnist_mlp import MLP
from hseam import workload
from kohakutpu.application.ktpugrad import call_capture as C
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D
from tinygrad import Tensor, tensor
from tinygrad.helpers import Context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workload", choices=("vit-eager", "lenet-eager", "mlp"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with (
        D.attached(D.Device("offline", board="multimesh_v9t6", queue_depth=1)) as dev,
        Context(TRAINING=int(args.workload == "lenet-eager")),
    ):
        if args.workload == "mlp":
            with Context(DEV="CPU"):
                model = MLP(128, 784)
            place(model)
            execute = lambda x: model(x).realize()
            arrays = (np.ones((1, 784), dtype=np.float16),)
        else:
            execute, arrays = workload(args.workload)
        inputs = tuple(Tensor(a, device="KTPU").realize() for a in arrays)
        for _ in range(3):
            execute(*inputs)
        graphs = []

        def observe(sink):
            graphs.append(sink)
            return C.ORIGINAL(sink)

        before = dev.counters["runs"]
        with patch.object(tensor, "transform_to_call", observe):
            execute(*inputs)
        launches = dev.counters["runs"] - before
        tables = {mode: C.Captures() for mode in (0, 1)}
        for mode, table in tables.items():
            os.environ["KTPU_NATIVE_CAPTURE"] = str(mode)
            for sink in graphs:
                table.call(sink)
        samples = {mode: [] for mode in tables}
        for turn in range(10):
            for mode in ((0, 1) if turn % 2 == 0 else (1, 0)):
                os.environ["KTPU_NATIVE_CAPTURE"] = str(mode)
                start = time.process_time_ns()
                for sink in graphs:
                    tables[mode].call(sink)
                samples[mode].append((time.process_time_ns() - start) / 1e6)
        result = {
            "workload": args.workload,
            "realizations": len(graphs),
            "launches": launches,
            "modes": {
                mode: {
                    "samples_ms": s,
                    "median_ms": statistics.median(s),
                    "ms_per_launch": statistics.median(s) / launches,
                    "counts": dict(tables[mode].counts),
                }
                for mode, s in samples.items()
            },
        }
        dev.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
