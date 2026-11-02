"""ViT requests on the card's nodes: one node, one resident copy on each node
with independent requests, one batch split across the nodes.

Prints one JSON object: wall seconds per request of each mode, their ratio to
one node, each round's logit error against a CPU run, and the device counters.
"""

import argparse
import json
import time

import numpy as np
from examples.tinygrad.vit_tiny import seeded_model
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D

WARM_ROUNDS = 3


def tensor(x):
    return Tensor(x.astype(np.float16), device="KTPU")


def rounds(run, count):
    """Wall seconds of the last `count` calls of `run`, past its warm rounds; the
    outputs of every round."""
    outs = [run() for _ in range(WARM_ROUNDS)]
    t0 = time.monotonic()
    timed = [run() for _ in range(count)]
    return (time.monotonic() - t0) / count, outs + timed


def error(got, want):
    return float(np.abs(got - want).max() / max(np.abs(want).max(), 1e-9))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--blocks", type=int, default=2)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--inputs", type=int, default=8, help="timed rounds")
    ap.add_argument(
        "--no-jit", action="store_true", help="realize each forward eagerly"
    )
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--board", default="multimesh_v9t6")
    ap.add_argument("--device", choices=("sim", "card"), default="sim")
    ap.add_argument("--transport", choices=D.TRANSPORTS, default=None)
    ap.add_argument("--nodes", type=int, default=None)
    ap.add_argument("--library", default=None)
    ap.add_argument("--library-only", action="store_true")
    args = ap.parse_args()
    dev = D.Device(
        args.device,
        board=args.board,
        nodes=args.nodes,
        transport=args.transport,
        library=args.library,
        library_only=args.library_only,
    )
    rng = np.random.default_rng(args.seed + 1)
    nodes = list(range(dev.nodes))
    xs = [
        rng.standard_normal((args.batch, 3, 224, 224)).astype(np.float32) for _ in nodes
    ]
    batch = np.concatenate(xs)
    ktpugrad.uninstall()
    want = seeded_model(args.blocks, args.seed)(Tensor(batch, device="CPU")).numpy()
    model = seeded_model(args.blocks, args.seed)
    ktpugrad.install()
    replay = not args.no_jit
    try:
        with D.attached(dev):
            place(model)
            one = ktpugrad.Inference(model, replay=replay)
            each = ktpugrad.Resident(model, nodes, replay=replay)
            split = ktpugrad.Sharded(model, nodes, replay=replay)

            def serial():
                return np.concatenate([one(tensor(x)).numpy() for x in xs])

            def resident():
                outs = each([(tensor(x),) for x in xs])
                return np.concatenate([out.numpy() for out in outs])

            def sharded():
                return split(tensor(batch)).numpy()

            modes = {"one": serial, "resident": resident, "sharded": sharded}
            result = {"nodes": dev.nodes, "blocks": args.blocks, "batch": args.batch}
            for name, run in modes.items():
                seconds, outs = rounds(run, args.inputs)
                errors = [error(out.astype(np.float32), want) for out in outs]
                result[name] = {"seconds": seconds / len(xs), "errors": errors}
            for name in ("resident", "sharded"):
                result[name]["speedup"] = (
                    result["one"]["seconds"] / result[name]["seconds"]
                )
            result["counters"] = dev.counters
            print(json.dumps(result, sort_keys=True))
    finally:
        dev.close()


if __name__ == "__main__":
    main()
