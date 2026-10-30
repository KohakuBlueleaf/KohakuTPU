"""Isolate one generated launch from tinygrad's graph-building CPU floor."""

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

from hseam_counting import CountingCard
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.native_launch import descriptor


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", choices=("offline", "counting"), default="counting")
    ap.add_argument("--repeat", type=int, default=1000)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    assert os.environ.get("KTPU_NATIVE_MEMORY") == "1"
    with patch.object(
        D, "OfflineCard", CountingCard if args.backend == "counting" else D.OfflineCard
    ):
        dev = D.Device("offline", board="multimesh_v9t6", queue_depth=1)
    kernel, given = V.flat_kernel(X.op("add", X.Load(0), 1.0), ("f16",), 128, "f16", 2)
    compiled = dev.specialise(kernel, given)
    names = compiled.l1.params(compiled.name, "l1")
    handles = [dev.alloc(4096) for _ in names]
    binds = dict(zip((p for p, _ in names), handles, strict=True))
    captured = []

    def capture(compiled, full, progs, s):
        captured.append(descriptor(dev, compiled, full, progs, s, binds))

    with patch.object(dev, "_recipe_capture", capture):
        dev.run(compiled, binds)
    native = captured[0]
    results = {}
    for mode in ("python", "native"):
        samples = []
        for _ in range(7):
            start = time.process_time_ns()
            for _ in range(args.repeat):
                if mode == "native":
                    dev.collect_through(native.launch(dev, handles))
                else:
                    dev.run(compiled, binds)
            samples.append((time.process_time_ns() - start) / args.repeat / 1e6)
        results[mode] = {
            "ms_per_launch": statistics.median(samples[1:]),
            "samples": samples,
        }
    results.update(backend=args.backend, repeat=args.repeat, numeric_execution=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results), flush=True)
    dev.close()


if __name__ == "__main__":
    main()
