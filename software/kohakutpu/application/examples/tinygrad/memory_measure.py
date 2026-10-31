"""Offline LeNet batch-8 allocation measurement, including device reservations."""

import json
from unittest.mock import patch

import numpy as np
from mnist_mlp import LeNet
from tinygrad.helpers import Context
from tinygrad.tensor import Tensor

from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.arena import Arena


def main():
    arenas = []
    counts = {"allocations": 0, "peak_bytes": 0}
    init, alloc, top = Arena.__init__, Arena.alloc, Arena.alloc_top

    def initialize(self, *args, **kwargs):
        init(self, *args, **kwargs)
        arenas.append(self)

    def measure(fn):
        def take(self, *args, **kwargs):
            at = fn(self, *args, **kwargs)
            counts["allocations"] += 1
            # A carved arena's bytes are already reserved in its parent.
            roots = [
                a
                for a in arenas
                if not any(a is not b and b.lo <= a.lo and a.hi <= b.hi for b in arenas)
            ]
            counts["peak_bytes"] = max(counts["peak_bytes"], sum(a.used for a in roots))
            return at

        return take

    with (
        patch.object(Arena, "__init__", initialize),
        patch.object(Arena, "alloc", measure(alloc)),
        patch.object(Arena, "alloc_top", measure(top)),
    ):
        dev = D.Device(kind="offline", board="multimesh_v9t6")
        with D.attached(dev), Context(DEV="CPU"):
            Tensor.manual_seed(1)
            model = LeNet()
            place(model)
            x = Tensor(np.zeros((8, 784), dtype=np.float16), device="KTPU").realize()
            model(x).realize()
            dev.drain()
        counts["runs"] = len(dev.runs)
        print(json.dumps(counts, sort_keys=True), flush=True)
        dev.close()


if __name__ == "__main__":
    main()
