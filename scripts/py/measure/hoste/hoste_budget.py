"""Prepared replay pin-all versus hot-set streaming, counting queue CPU and bytes."""

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
import argparse
import json
import statistics
import time
from pathlib import Path
from unittest.mock import patch

from hseam_counting import CountingCard
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.replay_budget import held_bytes


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    with patch.object(D, "OfflineCard", CountingCard):
        dev = D.Device("offline", board="multimesh_v9t6", queue_depth=2)
    # Disable the optional idle cache so streamed uploads are counted each replay.
    dev.card.q.resident_bytes = 0
    kernel, given = V.flat_kernel(X.op("add", X.Load(0), 1.0), ("f16",), 128, "f16", 2)
    compiled = dev.specialise(kernel, given)
    allocations = [
        {p: dev.alloc(4096) for p, _ in compiled.l1.params(compiled.name, "l1")}
        for _ in range(8)
    ]
    dev.record()
    for i in range(32):
        dev.run(compiled, allocations[0 if i % 2 == 0 else (i // 2) % 7 + 1])
    log = dev.recorded()
    dev.drain()
    results = {}
    for lists in (False, True):
        for label, budget in (
            ("all", 128 << 20),
            ("partial", 2176),
            ("stream", 0),
            ("auto", None),
        ):
            if log.prepared is not None:
                log.prepared.close()
                log.prepared = None
            dev.execution_list = lists
            dev.replay_package_bytes = budget
            start = time.process_time()
            dev.prepare_recorded(log)
            prepare_ms = (time.process_time() - start) * 1000
            counts = dict(dev.card.q.counters)
            samples = []
            for _ in range(8):
                start = time.process_time()
                dev.replay(log)
                samples.append((time.process_time() - start) * 1000 / 32)
            results[f"{label}-lists{int(lists)}"] = {
                "budget": log.prepared.budget,
                "selection": log.prepared.selection,
                "held": held_bytes(dev.card),
                "prepare_ms": prepare_ms,
                "median_ms_per_package": statistics.median(samples),
                "samples": samples,
                "counters": {k: v - counts[k] for k, v in dev.card.q.counters.items()},
            }
    log.close()
    dev.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results), flush=True)


if __name__ == "__main__":
    main()
