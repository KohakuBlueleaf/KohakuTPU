"""Bounded HOSTL integration measurement over the real runtime/counting queue."""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

from hseam_counting import CountingCard
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=64)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    with patch.object(D, "OfflineCard", CountingCard):
        dev = D.Device("offline", board="multimesh_v9t6", queue_depth=2)
    kernel, given = V.flat_kernel(X.op("add", X.Load(0), 1.0), ("f16",), 128, "f16", 2)
    compiled = dev.specialise(kernel, given)
    binds = {p: dev.alloc(4096) for p, _ in compiled.l1.params(compiled.name, "l1")}
    dev.record()
    for _ in range(args.count):
        dev.run(compiled, binds)
    log = dev.recorded()
    dev.drain()
    results = {}
    for mode in (False, True):
        if log.prepared is not None:
            log.prepared.close()
            log.prepared = None
        dev.execution_list = mode
        dev.prepare_recorded(log)
        samples = []
        before = dev.card.q.counters["submitted"]
        for _ in range(8):
            t = time.process_time_ns()
            dev.replay(log)
            samples.append((time.process_time_ns() - t) / 1e6 / args.count)
        results[str(mode)] = {
            "ms_per_package": samples,
            "sq_entries": dev.card.q.counters["submitted"] - before,
        }
    log.close()
    dev.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    main()
