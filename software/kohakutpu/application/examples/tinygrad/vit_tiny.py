"""ViT-tiny/16: seeded weights, 224x224 RGB, 197 tokens, 192 channels.

--offline compiles and verifies without claiming numeric results.
--device card --transport xdma --board multimesh_v9t6 runs on the card; with
--library DIR --library-only every kernel comes from the prebuilt library.
--blocks 1 selects one full encoder block; the default is twelve blocks.
--realize-ops retains operation/movement boundaries; the default lets the scheduler fuse.
--batch selects images per forward (1 by default).
--no-jit realizes each forward eagerly; the default replays it through TinyJit.
"""

import argparse
import json
import pathlib
import time

import numpy as np
from examples.tinygrad import perf_inputs
from examples.tinygrad.boundaries import boundary
from tinygrad import nn
from tinygrad.helpers import Context
from tinygrad.nn.state import get_state_dict
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.checks.accuracy import (
    DEFAULT_INPUTS,
    PER_IMAGE_SECONDS,
    error_stats,
    mxfp7_operands,
    output_report,
)
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D


class Block:
    def __init__(self, width=192, realize_ops=False):
        self.realize_ops = realize_ops
        self.width, self.heads = width, width // 64
        self.norm1 = nn.LayerNorm(width, eps=1e-6)
        self.qkv = nn.Linear(width, 3 * width)
        self.proj = nn.Linear(width, width)
        self.norm2 = nn.LayerNorm(width, eps=1e-6)
        self.fc1 = nn.Linear(width, 4 * width)
        self.fc2 = nn.Linear(4 * width, width)

    def __call__(self, x):
        batch, tokens, _ = x.shape
        norm = boundary(self.norm1(x), self.realize_ops)
        qkv = boundary(self.qkv(norm), self.realize_ops).reshape(
            batch, tokens, 3, self.heads, 64
        )
        q, k, v = (
            boundary(qkv[:, :, i].transpose(1, 2), self.realize_ops, contiguous=True)
            for i in range(3)
        )
        scores = boundary((q @ k.transpose(-1, -2)) * 0.125, self.realize_ops)
        a = boundary(
            boundary(scores.softmax(-1), self.realize_ops) @ v, self.realize_ops
        )
        a = boundary(a.transpose(1, 2), self.realize_ops, contiguous=True).reshape(
            batch, tokens, self.width
        )
        x = boundary(x + self.proj(a), self.realize_ops)
        hidden = boundary(
            self.fc1(boundary(self.norm2(x), self.realize_ops)).gelu(), self.realize_ops
        )
        return boundary(x + self.fc2(hidden), self.realize_ops)


class ViTTiny:
    def __init__(self, blocks=12, width=192, image_size=224, realize_ops=False):
        self.realize_ops = realize_ops
        self.width, self.grid = width, image_size // 16
        self.patch = nn.Linear(3 * 16 * 16, width)
        self.cls = Tensor.zeros(1, 1, width, device="CPU")
        self.pos = Tensor.zeros(1, self.grid**2 + 1, width, device="CPU")
        self.blocks = [Block(width, realize_ops) for _ in range(blocks)]
        self.norm = nn.LayerNorm(width, eps=1e-6)
        self.head = nn.Linear(width, 1000)

    def __call__(self, x, observe=None):
        batch = x.shape[0]
        p = boundary(
            x.reshape(batch, 3, self.grid, 16, self.grid, 16).permute(0, 2, 4, 1, 3, 5),
            self.realize_ops,
            contiguous=True,
        )
        p = boundary(self.patch(p.reshape(batch, self.grid**2, 768)), self.realize_ops)
        x = boundary(
            self.cls.expand(batch, 1, self.width).cat(p, dim=1) + self.pos,
            self.realize_ops,
        )
        if observe is not None:
            observe("embedding", x)
        for index, block in enumerate(self.blocks):
            x = block(x)
            if observe is not None:
                observe(f"block_{index + 1}", x)
        normalized = boundary(self.norm(x), self.realize_ops)
        if observe is not None:
            observe("final_norm", normalized)
        return self.head(boundary(normalized[:, 0], self.realize_ops, contiguous=True))


def seeded_model(blocks, seed, width=192, image_size=224, realize_ops=False):
    """Numpy draws identical f32 weights independent of tinygrad RNG state."""
    rng = np.random.default_rng(seed)
    with Context(DEV="CPU"):
        model = ViTTiny(blocks, width, image_size, realize_ops)
        for name, t in get_state_dict(model).items():
            if "norm" in name:
                a = np.ones(t.shape) if name.endswith("weight") else np.zeros(t.shape)
            else:
                a = rng.standard_normal(t.shape) * 0.02
            t.replace(Tensor(a.astype(np.float32), device="CPU"))
    return model


def references(host, inputs, args) -> tuple[np.ndarray, np.ndarray]:
    """CPU f32 and simulated-MXFP7 logits of `inputs`, read from `args.references`
    when it holds this configuration's file, else computed (and written there)."""
    path = None
    if args.references is not None:
        name = (
            f"vit.w{args.width}.b{args.blocks}.i{args.image_size}.s{args.seed}"
            f".n{args.batch}x{args.inputs}.npz"
        )
        path = args.references / name
        if path.exists():
            saved = np.load(path)
            return saved["f32"], saved["mxfp7"]

    want = np.stack([host(Tensor(x, device="CPU")).numpy() for x in inputs])
    with mxfp7_operands():
        mx = np.stack([host(Tensor(x, device="CPU")).numpy() for x in inputs])
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, f32=want, mxfp7=mx)
    return want, mx


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--blocks", type=int, default=12)
    ap.add_argument(
        "--realize-ops", action="store_true", help="retain operation boundaries"
    )
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument(
        "--inputs",
        type=int,
        default=DEFAULT_INPUTS,
        help="input batches (default 48; measured 1.24 s/image applies only to batch 1)",
    )
    ap.add_argument(
        "--no-jit", action="store_true", help="realize each forward eagerly"
    )
    ap.add_argument("--width", type=int, default=192)
    ap.add_argument("--image-size", type=int, default=224)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument(
        "--trace-inputs",
        default="",
        help="comma-separated zero-based image indices for per-block CPU/card states; extra readbacks, no one-minute guarantee",
    )
    ap.add_argument("--report", type=pathlib.Path)
    ap.add_argument(
        "--references",
        type=pathlib.Path,
        help="directory of reference logits, one file a configuration",
    )
    ap.add_argument(
        "--references-only",
        action="store_true",
        help="write the reference file and stop before the device",
    )
    ap.add_argument("--board", default=None)
    ap.add_argument("--device", choices=("sim", "card"), default="sim")
    ap.add_argument("--transport", choices=D.TRANSPORTS, default=None)
    ap.add_argument("--nodes", type=int, default=None)
    ap.add_argument(
        "--library", default=None, help="prebuilt kernel library to look kernels up in"
    )
    ap.add_argument(
        "--library-only",
        action="store_true",
        help="refuse to compile: every kernel must be a library hit",
    )
    args = ap.parse_args()
    if not 1 <= args.blocks <= 12 or args.inputs < 1 or args.batch < 1:
        ap.error("blocks must be 1..12 and inputs/batch positive")
    if (
        args.width < 64
        or args.width % 64
        or args.image_size < 16
        or args.image_size % 16
    ):
        ap.error(
            "width must be a positive multiple of 64; image-size a positive multiple of 16"
        )
    try:
        traced = {int(i) for i in args.trace_inputs.split(",") if i.strip()}
    except ValueError:
        ap.error("trace-inputs must contain integer indices")
    if any(i < 0 or i >= args.inputs for i in traced):
        ap.error("trace-inputs must be within 0..inputs-1")
    board = args.board or ("multimesh_v9t6" if args.offline else "vlt_v9t6s_1n")
    dev = D.Device(
        "offline" if args.offline else args.device,
        board=board,
        nodes=args.nodes,
        transport=args.transport,
        library=args.library,
        library_only=args.library_only,
    )
    rng = np.random.default_rng(args.seed + 1)
    inputs = [
        rng.standard_normal((args.batch, 3, args.image_size, args.image_size)).astype(
            np.float32
        )
        for _ in range(args.inputs)
    ]
    ktpugrad.uninstall()
    host = seeded_model(
        args.blocks, args.seed, args.width, args.image_size, args.realize_ops
    )
    host_states = {}
    for index in sorted(traced):
        states = host_states[index] = {}

        def capture(stage, value, states=states):
            states[stage] = value.numpy().copy()

        host(Tensor(inputs[index], device="CPU"), capture)
    want = mx = np.zeros(0)
    if args.references_only or not args.offline:
        want, mx = references(host, inputs, args)
    if args.references_only:
        return
    model = seeded_model(
        args.blocks, args.seed, args.width, args.image_size, args.realize_ops
    )
    ktpugrad.install()
    try:
        with D.attached(dev):
            place(model)
            predict = ktpugrad.Inference(model, replay=not args.no_jit)
            got, seconds, block_errors = [], [], []
            for index, x in enumerate(inputs):
                t0 = time.monotonic()
                stages = []

                def compare_state(stage, value, index=index, stages=stages):
                    metrics = error_stats(value.numpy(), host_states[index][stage])
                    previous = stages[-1]["absolute"]["max"] if stages else 0
                    stages.append(
                        dict(
                            stage=stage,
                            absolute_error_growth=metrics["absolute"]["max"] - previous,
                            **metrics,
                        )
                    )

                observer = (
                    compare_state if index in traced and not args.offline else None
                )
                with perf_inputs.span(index):
                    input_tensor = Tensor(x.astype(np.float16), device="KTPU")
                    y = (
                        model(input_tensor, observer).realize()
                        if observer is not None
                        else predict(input_tensor)
                    )
                    if stages:
                        block_errors.append(
                            {
                                "input": index,
                                "stages": stages,
                                "largest_absolute_growth_stage": max(
                                    stages, key=lambda r: r["absolute_error_growth"]
                                )["stage"],
                                "largest_block_growth_stage": max(
                                    (
                                        r
                                        for r in stages
                                        if r["stage"].startswith("block_")
                                    ),
                                    key=lambda r: r["absolute_error_growth"],
                                )["stage"],
                            }
                        )
                    if not args.offline:
                        got.append(y.numpy().astype(np.float32))
                        if stages:
                            block_errors[-1]["logits"] = error_stats(
                                got[-1], want[index]
                            )
                seconds.append(time.monotonic() - t0)
            result = {
                "seconds": seconds,
                "counters": dev.counters,
                "mode": dev.kind,
                "blocks": args.blocks,
                "batch": args.batch,
                "realize_ops": args.realize_ops,
                "width": args.width,
                "tokens": (args.image_size // 16) ** 2 + 1,
                "inputs": args.inputs,
                "kernels": len(dev.runs),
                "input_budget_seconds_per_image": PER_IMAGE_SECONDS,
                "images": args.inputs * args.batch,
                "input_budget_seconds": args.inputs * args.batch * PER_IMAGE_SECONDS,
                "block_errors": block_errors,
                "block_error_status": (
                    "measured"
                    if block_errors
                    else "not measured (offline or no trace-inputs)"
                ),
                "block_error_semantics": "cumulative states after the same seeded image; CPU f32 weights/input vs card f16 input and placed weights; not isolated-block error",
                "trace_inputs": sorted(traced),
                "check": (
                    None
                    if args.offline
                    else output_report(
                        np.concatenate(got), np.concatenate(want), np.concatenate(mx)
                    )
                ),
            }
            print(json.dumps(result, sort_keys=True))
            if args.report:
                args.report.write_text(json.dumps(result), encoding="utf-8")
    finally:
        dev.close()


if __name__ == "__main__":
    main()
