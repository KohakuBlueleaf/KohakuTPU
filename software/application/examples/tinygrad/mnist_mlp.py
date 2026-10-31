"""MNIST model family with host-buffer LR schedules and library-only training.

``--reference`` runs the same shuffled recipe entirely on CPU in f32, without
attaching an accelerator. Evaluation pads the last batch on the host so one
captured forward shape covers all 10,000 test images.
"""

import argparse
import json
import math
import pathlib
import time

import numpy as np
from examples.tinygrad.mnist_models import MLP, MODELS, LeNet  # noqa: F401
from tinygrad.dtype import _to_np_dtype
from tinygrad.engine.jit import TinyJit
from tinygrad.helpers import Context
from tinygrad.nn.datasets import mnist
from tinygrad.nn.optim import AdamW
from tinygrad.nn.state import get_parameters
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as card


def learning_rate(step, steps, lr, schedule="constant", warmup=0, lr_min=0.0):
    """Warmup counts steps; cosine/onecycle reach lr_min on the final update."""
    if warmup and step < warmup:
        return lr_min + (lr - lr_min) * (step + 1) / warmup
    progress = (step - warmup) / max(1, steps - warmup - 1)
    if schedule == "constant":
        return lr
    if schedule == "cosine":
        return lr_min + (lr - lr_min) * (1 + math.cos(math.pi * progress)) / 2
    if schedule == "onecycle":
        peak = 0.3
        factor = (
            (1 - math.cos(math.pi * progress / peak)) / 2
            if progress < peak
            else (1 + math.cos(math.pi * (progress - peak) / (1 - peak))) / 2
        )
        return lr_min + (lr - lr_min) * factor
    raise ValueError(f"unknown schedule: {schedule}")


class HostLearningRate:
    """Write the optimizer's existing realized buffer, outside the JIT graph.

    This tinygrad version exposes Buffer copyin as allocator._copyin; no assign,
    tensor arithmetic, or compute launch is needed to change the learning rate.
    """

    def __init__(self, opt):
        self.buffer = opt.lr.realize().uop.buffer
        self.values = np.empty(self.buffer.size, dtype=_to_np_dtype(self.buffer.dtype))

    def write(self, value):
        self.values.fill(value)
        self.buffer.allocator._copyin(
            self.buffer._buf, memoryview(self.values).cast("B")
        )
        return float(self.values[0])

    def read(self):
        return float(self.buffer.numpy()[0])


def epoch_batches(length, batch, seed=0):
    """End-to-end shuffled epochs, including the tail before the next shuffle."""
    rng = np.random.default_rng(seed)
    while True:
        order = rng.permutation(length)
        for start in range(0, length, batch):
            yield order[start : start + batch]


def train_step(model, opt, xb: Tensor, yb: Tensor):
    logits = model(xb)
    loss = -(logits.log_softmax() * yb).sum(-1).mean()
    opt.zero_grad()
    (opt.scale(loss) if isinstance(opt, Adam16) else loss).backward()
    Tensor.realize(loss, logits, *opt.schedule_step())
    return loss, logits


def evaluate(model, x, y, device, batch):
    """Host metrics over every example; the device runs only the model forward."""
    hits, total_loss = 0, 0.0
    predict = ktpugrad.Inference(model) if device == "KTPU" else model
    with Context(TRAINING=0):
        for i in range(0, len(x), batch):
            count = min(batch, len(x) - i)
            xb = np.zeros((batch, 784), dtype=x.dtype)
            xb[:count] = x[i : i + count]
            logits = (
                predict(Tensor(xb, device=device)).numpy()[:count].astype(np.float64)
            )
            hits += int((logits.argmax(1) == y[i : i + count]).sum())
            shifted = logits - logits.max(1, keepdims=True)
            total_loss += float(
                (
                    np.log(np.exp(shifted).sum(1))
                    - shifted[np.arange(count), y[i : i + count]]
                ).sum()
            )
    return {"accuracy": hits / len(x), "loss": total_loss / len(x), "images": len(x)}


def accuracy(model, x, y, device, batch=256):
    return evaluate(model, x, y, device, batch)["accuracy"]


def train(
    model,
    opt,
    x,
    y,
    steps,
    batch,
    device,
    log,
    jit,
    schedule="constant",
    warmup=0,
    lr_min=0.0,
    lr=2e-3,
    test_data=None,
    eval_batch=None,
):
    """One recipe for any tensor-LR optimizer, eager or TinyJit replay."""
    batches = epoch_batches(len(x), batch)
    eye = np.eye(10, dtype=x.dtype)
    host_lr = HostLearningRate(opt)
    curves = {"train": [], "test": []}
    start = time.monotonic()

    def one(xb, yb):
        return train_step(model, opt, xb, yb)

    # A short epoch-tail uses its own shape, shared by all epochs.
    runs = {}
    for step in range(steps):
        idx = next(batches)
        if len(idx) not in runs:
            runs[len(idx)] = TinyJit(one) if jit else one
        value = host_lr.write(learning_rate(step, steps, lr, schedule, warmup, lr_min))
        with Context(TRAINING=1):
            loss, logits = runs[len(idx)](
                Tensor(x[idx], device=device).realize(),
                Tensor(eye[y[idx]], device=device).realize(),
            )
        if step % log == 0 or step == steps - 1:
            record = {
                "step": step + 1,
                "lr": value,
                "loss": float(loss.item()),
                "accuracy": float((logits.numpy().argmax(1) == y[idx]).mean()),
                "seconds": time.monotonic() - start,
            }
            curves["train"].append(record)
            print(
                f"step {step + 1}: loss {record['loss']:.4f}, train accuracy {record['accuracy']:.4f}, lr {value:.8g}",
                flush=True,
            )
        if test_data is not None and (
            (step + 1) % math.ceil(len(x) / batch) == 0 or step == steps - 1
        ):
            metrics = evaluate(model, *test_data, device, eval_batch or batch)
            curves["test"].append(dict(step=step + 1, **metrics))
            print(f"test {step + 1}: {metrics}", flush=True)
    return curves


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", choices=sorted(MODELS), default="mlp")
    duration = ap.add_mutually_exclusive_group()
    duration.add_argument("--steps", type=int, default=None)
    duration.add_argument("--epochs", type=int)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--eval-batch", type=int, default=None)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument(
        "--schedule", choices=("constant", "cosine", "onecycle"), default="constant"
    )
    ap.add_argument("--warmup", type=int, default=0)
    ap.add_argument("--lr-min", type=float, default=0.0)
    ap.add_argument("--weight-decay", type=float, default=0.0)
    ap.add_argument(
        "--test",
        type=int,
        default=10000,
        help="test images (default: complete test set)",
    )
    ap.add_argument("--log", type=int, default=100)
    ap.add_argument(
        "--reference", action="store_true", help="CPU-only f32 reference, same recipe"
    )
    ap.add_argument("--report", type=pathlib.Path)
    ap.add_argument("--device", choices=("sim", "card"), default=None)
    ap.add_argument("--transport", choices=card.TRANSPORTS, default=None)
    ap.add_argument("--board", default=None)
    ap.add_argument("--nodes", type=int, default=None)
    ap.add_argument("--no-jit", action="store_true")
    ap.add_argument("--realize-ops", action="store_true")
    ap.add_argument("--library")
    ap.add_argument("--library-only", action="store_true")
    args = ap.parse_args()
    if (
        args.batch <= 0
        or (args.eval_batch is not None and args.eval_batch <= 0)
        or args.log <= 0
    ):
        ap.error("batch sizes and log interval must be positive")
    device = "CPU" if args.reference else "KTPU"
    rt = None
    if args.reference:
        ktpugrad.uninstall()
    else:
        rt = card.attach(
            card.Device(
                kind=args.device,
                nodes=args.nodes,
                board=args.board,
                transport=args.transport,
                library=args.library,
                library_only=args.library_only,
            )
        )
        print(rt)
    xtr, ytr, xte, yte = (t.numpy() for t in mnist(device="CPU"))
    dtype = np.float32 if args.reference else np.float16
    xtr = (xtr.reshape(-1, 784) / 255.0).astype(dtype)
    xte = (xte.reshape(-1, 784) / 255.0).astype(dtype)[: args.test]
    yte = yte[: args.test]
    steps = (
        args.steps
        if args.steps is not None
        else (
            args.epochs * math.ceil(len(xtr) / args.batch)
            if args.epochs is not None
            else 4
        )
    )
    if steps <= 0 or not 0 <= args.warmup < steps:
        ap.error("steps must be positive and 0 <= warmup < steps")
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = MODELS[args.model]()
        model.realize_ops = args.realize_ops
    if not args.reference:
        place(model)
    opt = (
        AdamW(
            get_parameters(model), lr=args.lr, b2=0.95, weight_decay=args.weight_decay
        )
        if args.reference
        else Adam16(get_parameters(model), lr=args.lr, weight_decay=args.weight_decay)
    )
    curves = train(
        model,
        opt,
        xtr,
        ytr,
        steps,
        args.batch,
        device,
        args.log,
        not args.no_jit,
        args.schedule,
        args.warmup,
        args.lr_min,
        args.lr,
        (xte, yte),
        args.eval_batch or args.batch,
    )
    result = {
        "model": args.model,
        "device": device,
        "steps": steps,
        "batch": args.batch,
        "realize_ops": args.realize_ops,
        "eval_batch": args.eval_batch or args.batch,
        "schedule": args.schedule,
        "warmup": args.warmup,
        "lr": args.lr,
        "lr_min": args.lr_min,
        "weight_decay": args.weight_decay,
        "curves": curves,
    }
    if rt is not None:
        result["counters"] = rt.counters
        print(f"host traffic {rt.counters}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
