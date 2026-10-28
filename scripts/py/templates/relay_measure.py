"""Offline library-only launch counts and warm launch host time by kind."""

import argparse
import json
import pathlib
import time
from collections import defaultdict
from unittest.mock import patch

import prebuild
from tinygrad.tensor import Tensor

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import generated as G


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model", choices=prebuild.MODELS)
    ap.add_argument("--library", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--steps", type=int, default=8)
    args = ap.parse_args()
    dev = D.attach(
        D.Device(
            kind="offline",
            board="multimesh_v9t6",
            library=args.library,
            library_only=True,
        )
    )
    current, steps = defaultdict(lambda: [0, 0.0]), []
    depth = 0

    def timed(fn):
        def call(*a, kind="dsl", **kw):
            nonlocal depth
            depth += 1
            start = time.perf_counter()
            try:
                return fn(*a, kind=kind, **kw)
            finally:
                depth -= 1
                if not depth:
                    current[kind][0] += 1
                    current[kind][1] += time.perf_counter() - start

        return call

    original = Tensor.item

    def item(tensor):
        result = original(tensor)
        steps.append(dict(current))
        current.clear()
        return result

    with (
        patch.object(FM, "launch", timed(FM.launch)),
        patch.object(G, "launch", timed(G.launch)),
        patch.object(Tensor, "item", item),
    ):
        records = prebuild.MODELS[args.model](
            args.batch, args.model != "lenet", args.steps
        )
    dev.drain()
    summary = {}
    warm = steps[1:]
    for kind in sorted({k for step in warm for k in step}):
        count = sum(step.get(kind, [0, 0])[0] for step in warm)
        seconds = sum(step.get(kind, [0, 0])[1] for step in warm)
        summary[kind] = dict(
            launches=count, ms_per_launch=1000 * seconds / max(count, 1)
        )
    result = dict(
        steps=records, kinds_by_step=steps, warm_by_kind=summary, counters=dev.counters
    )
    assert dev.counters["compile_calls"] == dev.counters["library_misses"] == 0
    pathlib.Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(summary))
    print(
        "last step",
        {k: v[0] for k, v in steps[-1].items()} if steps else "see run step launches",
    )


if __name__ == "__main__":
    main()
