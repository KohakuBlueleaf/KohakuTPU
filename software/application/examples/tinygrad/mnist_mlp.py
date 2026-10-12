"""An MNIST MLP (784 -> 128 -> 10) trained AND run on the KohakuTPU simulator.

    python software/application/examples/tinygrad/mnist_mlp.py

The model is a tinygrad module (`nn.Linear`), initialised on the host and
placed on the card in f16 (`ktpugrad.nn.place`). Every training step --
forward, backward and the optimizer -- is card kernels: `Adam16` keeps m,
sqrt(v) and a Kahan error buffer in f16 beside the f16 weights, and the loss
is scaled before `backward` so small gradients survive f16. Products run in
MXFP7. The script prints the loss, the test accuracy on the card and the
card cycles; ``--reference`` trains the same model in f32 on the CPU. The
defaults are a short run on the simulator (a step is ~1.4 M card cycles);
``--steps`` / ``--test`` set a full one.
"""

import argparse
import time

import numpy as np
from tinygrad import nn
from tinygrad.helpers import Context
from tinygrad.nn.datasets import mnist
from tinygrad.nn.optim import Adam
from tinygrad.nn.state import get_parameters, get_state_dict, load_state_dict
from tinygrad.tensor import Tensor

from kohakutpu.application import ktpugrad
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as card


class MLP:
    def __init__(self, hidden: int = 128) -> None:
        self.l1 = nn.Linear(784, hidden)
        self.l2 = nn.Linear(hidden, 10)

    def __call__(self, x: Tensor) -> Tensor:
        return self.l2(self.l1(x).relu())


def train(model, opt, x, y, steps: int, batch: int, device: str, log: int) -> None:
    """`steps` of `opt` on `model` over random batches; the loss is read back
    every `log` steps."""
    rng = np.random.default_rng(0)
    t0 = time.monotonic()

    with Context(TRAINING=1):
        for step in range(steps):
            idx = rng.integers(0, len(x), batch)
            xb = Tensor(x[idx], device=device)
            yb = Tensor(y[idx].astype(np.int32), device=device)

            loss = model(xb).sparse_categorical_crossentropy(yb)
            opt.zero_grad()
            (opt.scale(loss) if isinstance(opt, Adam16) else loss).backward()
            # The loss with the step, in one schedule: read before the write.
            Tensor.realize(loss, *opt.schedule_step())

            if step % log == 0 or step == steps - 1:
                wall = time.monotonic() - t0
                print(
                    f"  step {step:4d}  loss {loss.item():.4f}  ({wall:.0f} s)",
                    flush=True,
                )


def accuracy(model, x, y, device: str, batch: int = 256) -> float:
    hits = 0
    for i in range(0, len(x), batch):
        logits = model(Tensor(x[i : i + batch], device=device)).numpy()
        hits += int((logits.argmax(1) == y[i : i + batch]).sum())
    return hits / len(x)


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--steps", type=int, default=4, help="training steps on the card")
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--lr", type=float, default=2e-3)
    ap.add_argument("--test", type=int, default=64, help="test images classified")
    ap.add_argument("--log", type=int, default=1, help="read the loss every N steps")
    ap.add_argument(
        "--reference", action="store_true", help="also train in f32 on the CPU"
    )
    args = ap.parse_args()

    xtr, ytr, xte, yte = (t.numpy() for t in mnist(device="CPU"))
    xtr = (xtr.reshape(-1, 784) / 255.0).astype(np.float16)
    xte = (xte.reshape(-1, 784) / 255.0).astype(np.float16)[: args.test]
    yte = yte[: args.test]

    # Initialise on the host, then put every tensor on the card in f16.
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = MLP()
    init = {k: v.numpy() for k, v in get_state_dict(model).items()}
    place(model)

    rt = card.device()
    print(f"training on KTPU: {args.steps} steps of {args.batch}, Adam16")
    t0 = time.monotonic()
    opt = Adam16(get_parameters(model), lr=args.lr)
    train(model, opt, xtr, ytr, args.steps, args.batch, "KTPU", args.log)
    runs, cycles = len(rt.runs), sum(r.cycles for r in rt.runs)
    print(
        f"  {runs} kernel runs, {cycles:,} card cycles, {time.monotonic() - t0:.0f} s wall"
    )

    acc = accuracy(model, xte, yte, "KTPU")
    more = sum(r.cycles for r in rt.runs) - cycles
    print(f"KTPU test accuracy ({len(xte)} images): {acc:.4f}; {more:,} cycles")

    if args.reference:
        ktpugrad.uninstall()  # tinygrad's own lowering, f32 by default
        ref = MLP()
        load_state_dict(ref, {k: Tensor(v.astype(np.float32)) for k, v in init.items()})

        print("reference: the same model and batches, f32 Adam on the CPU")
        train(
            ref,
            Adam(get_parameters(ref), lr=args.lr),
            xtr.astype(np.float32),
            ytr,
            args.steps,
            args.batch,
            "CPU",
            args.log,
        )
        print(
            f"CPU test accuracy: {accuracy(ref, xte.astype(np.float32), yte, 'CPU'):.4f}"
        )


if __name__ == "__main__":
    main()
