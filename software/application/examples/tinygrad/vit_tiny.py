"""ViT-tiny/16: seeded weights, 224x224 RGB, 197 tokens, 192 channels.

--offline compiles and verifies without claiming numeric results.
--device card --transport xdma --board multimesh_v9t6 runs on the card; with
--library DIR --library-only every kernel comes from the prebuilt library.
--blocks 1 selects one full encoder block; the default is twelve blocks.
"""

import argparse
import json
import time

import numpy as np
from tinygrad import Tensor, nn
from tinygrad.helpers import Context
from tinygrad.nn.state import get_state_dict

from kohakutpu.application import ktpugrad
from kohakutpu.application.checks.accuracy import (
    DEFAULT_INPUTS,
    PER_IMAGE_SECONDS,
    depth_budget,
    logit_report,
    state_error,
)
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D


class Block:
    def __init__(self, width=192):
        self.width, self.heads = width, width // 64
        self.norm1 = nn.LayerNorm(width, eps=1e-6)
        self.qkv = nn.Linear(width, 3 * width)
        self.proj = nn.Linear(width, width)
        self.norm2 = nn.LayerNorm(width, eps=1e-6)
        self.fc1 = nn.Linear(width, 4 * width)
        self.fc2 = nn.Linear(4 * width, width)

    def __call__(self, x):
        batch, tokens, _ = x.shape
        qkv = (
            self.qkv(self.norm1(x).realize())
            .realize()
            .reshape(batch, tokens, 3, self.heads, 64)
        )
        q, k, v = (
            qkv[:, :, i].transpose(1, 2).contiguous().realize() for i in range(3)
        )
        scores = ((q @ k.transpose(-1, -2)) * 0.125).realize()
        a = (scores.softmax(-1).realize() @ v).realize()
        a = a.transpose(1, 2).contiguous().realize().reshape(batch, tokens, self.width)
        x = (x + self.proj(a)).realize()
        hidden = self.fc1(self.norm2(x).realize()).gelu().realize()
        return (x + self.fc2(hidden)).realize()


class ViTTiny:
    def __init__(self, blocks=12, width=192, image_size=224):
        self.width, self.grid = width, image_size // 16
        self.patch = nn.Linear(3 * 16 * 16, width)
        self.cls = Tensor.zeros(1, 1, width, device="CPU")
        self.pos = Tensor.zeros(1, self.grid**2 + 1, width, device="CPU")
        self.blocks = [Block(width) for _ in range(blocks)]
        self.norm = nn.LayerNorm(width, eps=1e-6)
        self.head = nn.Linear(width, 1000)

    def __call__(self, x, observe=None):
        batch = x.shape[0]
        p = (
            x.reshape(batch, 3, self.grid, 16, self.grid, 16)
            .permute(0, 2, 4, 1, 3, 5)
            .contiguous()
            .realize()
        )
        p = self.patch(p.reshape(batch, self.grid**2, 768)).realize()
        x = (self.cls.expand(batch, 1, self.width).cat(p, dim=1) + self.pos).realize()
        if observe is not None:
            observe("embedding", x)
        for index, block in enumerate(self.blocks):
            x = block(x)
            if observe is not None:
                observe(f"block_{index + 1}", x)
        normalized = self.norm(x).realize()
        if observe is not None:
            observe("final_norm", normalized)
        return self.head(normalized[:, 0].contiguous().realize())


def seeded_model(blocks, seed, width=192, image_size=224):
    """Numpy draws identical f32 weights independent of tinygrad RNG state."""
    rng = np.random.default_rng(seed)
    with Context(DEV="CPU"):
        model = ViTTiny(blocks, width, image_size)
        for name, t in get_state_dict(model).items():
            if "norm" in name:
                a = np.ones(t.shape) if name.endswith("weight") else np.zeros(t.shape)
            else:
                a = rng.standard_normal(t.shape) * 0.02
            t.replace(Tensor(a.astype(np.float32), device="CPU"))
    return model


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--blocks", type=int, default=12)
    ap.add_argument(
        "--inputs",
        type=int,
        default=DEFAULT_INPUTS,
        help="images (default 48, about 60 s at the measured 1.24 s/image)",
    )
    ap.add_argument("--width", type=int, default=192)
    ap.add_argument("--image-size", type=int, default=224)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument(
        "--trace-inputs",
        default="",
        help="comma-separated zero-based image indices for per-block CPU/card states; extra readbacks, no one-minute guarantee",
    )
    ap.add_argument(
        "--logit-relative-budget",
        type=float,
        default=0.03,
        help="diagnostic relative threshold, NOT a certified network bound",
    )
    ap.add_argument("--cosine-min", type=float, default=0.999)
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
    if not 1 <= args.blocks <= 12 or args.inputs < 1:
        ap.error("blocks must be 1..12 and inputs positive")
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
    if (
        not np.isfinite(args.logit_relative_budget)
        or args.logit_relative_budget <= 0
        or not -1 <= args.cosine_min <= 1
    ):
        ap.error("positive finite logit budget and cosine threshold in [-1,1] required")
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
        rng.standard_normal((1, 3, args.image_size, args.image_size)).astype(np.float32)
        for _ in range(args.inputs)
    ]
    ktpugrad.uninstall()
    host = seeded_model(args.blocks, args.seed, args.width, args.image_size)
    want, host_states = [], {}
    if not args.offline:
        for index, x in enumerate(inputs):
            states = {}

            def capture(stage, value, states=states):
                states[stage] = value.numpy().copy()

            want.append(
                host(
                    Tensor(x, device="CPU"), capture if index in traced else None
                ).numpy()
            )
            if index in traced:
                host_states[index] = states
    model = seeded_model(args.blocks, args.seed, args.width, args.image_size)
    ktpugrad.install()
    try:
        with D.attached(dev):
            place(model)
            predict = ktpugrad.Inference(model)
            got, seconds, block_errors = [], [], []
            for index, x in enumerate(inputs):
                t0 = time.monotonic()
                stages = []

                def compare_state(stage, value, index=index, stages=stages):
                    metrics = state_error(value.numpy(), host_states[index][stage])
                    previous = stages[-1]["max_absolute_error"] if stages else 0
                    stages.append(
                        dict(
                            stage=stage,
                            absolute_error_growth=metrics["max_absolute_error"]
                            - previous,
                            **metrics,
                        )
                    )

                observer = (
                    compare_state if index in traced and not args.offline else None
                )
                input_tensor = Tensor(x.astype(np.float16), device="KTPU")
                y = (
                    model(input_tensor, observer).realize()
                    if observer is not None
                    else predict(input_tensor)
                )
                if stages:
                    block_errors.append(
                        dict(
                            input=index,
                            stages=stages,
                            largest_absolute_growth_stage=max(
                                stages, key=lambda r: r["absolute_error_growth"]
                            )["stage"],
                            largest_block_growth_stage=max(
                                (r for r in stages if r["stage"].startswith("block_")),
                                key=lambda r: r["absolute_error_growth"],
                            )["stage"],
                        )
                    )
                if not args.offline:
                    got.append(y.numpy().astype(np.float32))
                    if stages:
                        block_errors[-1]["logits"] = state_error(got[-1], want[index])
                seconds.append(time.monotonic() - t0)
            result = {
                "seconds": seconds,
                "counters": dev.counters,
                "mode": dev.kind,
                "blocks": args.blocks,
                "width": args.width,
                "tokens": (args.image_size // 16) ** 2 + 1,
                "inputs": args.inputs,
                "kernels": len(dev.runs),
                "input_budget_seconds_per_image": PER_IMAGE_SECONDS,
                "input_budget_seconds": args.inputs * PER_IMAGE_SECONDS,
                "numerics_budget": depth_budget(args.blocks),
                "block_errors": block_errors,
                "block_error_status": (
                    "measured"
                    if block_errors
                    else "not measured (offline or no trace-inputs)"
                ),
                "block_error_semantics": "cumulative states after the same seeded image; CPU f32 weights/input vs card f16 input and placed weights; not isolated-block error",
                "trace_inputs": sorted(traced),
            }
            if args.offline:
                result.update(
                    max_relative_error=None,
                    top1_agreement=None,
                    per_input=None,
                    decided_inputs=None,
                    undecided_inputs=None,
                    decided_top1_agreement=None,
                    top5_set_agreement=None,
                    top5_mean_overlap=None,
                )
            else:
                g, w = np.concatenate(got), np.concatenate(want)
                result["max_relative_error"] = float(
                    np.abs(g - w).max() / max(float(np.abs(w).max()), 1e-9)
                )
                result["top1_agreement"] = float((g.argmax(-1) == w.argmax(-1)).mean())
                result.update(
                    logit_report(g, w, args.logit_relative_budget, args.cosine_min)
                )
                result["numerics_budget"] = depth_budget(
                    args.blocks, result["max_relative_error"]
                )
            print(json.dumps(result, sort_keys=True))
    finally:
        dev.close()


if __name__ == "__main__":
    main()
