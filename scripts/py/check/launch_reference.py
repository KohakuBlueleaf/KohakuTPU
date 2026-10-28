"""CPU-f32 network + the actual Adam16 graph, narrowed only at optimizer stores.

The optimizer class/equations are not reimplemented. Its assignment destinations
and contiguous y/new intermediates use f16 storage represented as f32 CPU tensors.
This is not an emulator of the accelerator's network or MXFP7 arithmetic.
"""

import argparse
import json
import runpy
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import numpy as np
from tinygrad import Tensor
from tinygrad.dtype import dtypes
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters

from kohakutpu.application.ktpugrad.nn import Adam16, place

ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def optimizer_storage(opt):
    assign, contiguous = Tensor.assign, Tensor.contiguous
    destinations = {
        id(t) for t in [*opt.params, *opt.m, *opt.rms, *opt.err, opt.b1_t, opt.b2_t]
    }

    def narrow(t):
        return contiguous(t.cast(dtypes.half)).cast(dtypes.float)

    def store(self, value):
        return assign(self, narrow(value) if id(self) in destinations else value)

    def intermediate(self, *args, **kwargs):
        return contiguous(narrow(self), *args, **kwargs)

    with (
        patch.object(Tensor, "assign", store),
        patch.object(Tensor, "contiguous", intermediate),
    ):
        yield


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=2)
    ap.add_argument("--batch", type=int, default=8)
    args = ap.parse_args()
    rng = np.random.default_rng(123)
    ex = runpy.run_path(
        str(ROOT / "software/application/examples/tinygrad/mnist_mlp.py")
    )
    with Context(DEV="CPU"):
        Tensor.manual_seed(0)
        model = ex["LeNet"]()
    place(model, "CPU", dtypes.half)
    place(model, "CPU", dtypes.float)
    params = get_parameters(model)
    with Context(DEFAULT_FLOAT="float"):
        opt = Adam16(params, lr=float(np.float16(2e-3)))
    arrays = {}
    with Context(TRAINING=1):
        for step in range(args.steps):
            raw = (rng.standard_normal((args.batch, 784)) * 0.1).astype(np.float16)
            labels = np.eye(10, dtype=np.float16)[np.arange(args.batch) % 10]
            x, y = Tensor(raw.astype(np.float32), device="CPU"), Tensor(
                labels.astype(np.float32), device="CPU"
            )
            loss = -(model(x).log_softmax() * y).sum(-1).mean()
            opt.zero_grad()
            opt.scale(loss).backward()
            with optimizer_storage(opt):
                updates = opt.schedule_step()
            Tensor.realize(loss, *updates)
            arrays[f"step{step}/loss"] = loss.numpy().copy()
            for group, tensors in (
                ("param", params),
                ("m", opt.m),
                ("rms", opt.rms),
                ("err", opt.err),
            ):
                for i, tensor in enumerate(tensors):
                    a = tensor.numpy().copy()
                    assert np.array_equal(a, a.astype(np.float16).astype(np.float32)), (
                        step,
                        group,
                        i,
                        a.dtype,
                        float(
                            np.nanmax(
                                np.abs(a - a.astype(np.float16).astype(np.float32))
                            )
                        ),
                    )
                    arrays[f"step{step}/{group}/{i}"] = a
    dest = ROOT / ".kt/launch-loop2"
    dest.mkdir(parents=True, exist_ok=True)
    np.savez(dest / f"lenet-b{args.batch}-cpu-stored.npz", **arrays)
    (dest / "reference.json").write_text(
        json.dumps(
            dict(
                optimizer="Adam16.schedule_step unchanged",
                network="tinygrad CPU f32",
                initial_weights_inputs="f16-rounded then promoted f32",
                store_roundings="m,rms,err,params,b1_t,b2_t and contiguous y/new: f32->f16->f32",
                lr=float(np.float16(2e-3)),
                steps=args.steps,
                batch=args.batch,
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    print("CPU reference stored", len(arrays), "tensors")


if __name__ == "__main__":
    main()
