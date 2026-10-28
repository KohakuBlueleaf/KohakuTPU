"""Independent f32 ViT reference with the launch witness's initial storage values."""

import json
from pathlib import Path

import numpy as np
from examples.tinygrad.vit_tiny import seeded_model
from tinygrad import Tensor
from tinygrad.dtype import dtypes
from tinygrad.helpers import Context

from kohakutpu.application.ktpugrad.nn import place

ROOT = Path(__file__).resolve().parents[3]


def main():
    with Context(DEV="CPU"):
        Tensor.manual_seed(0)
        model = seeded_model(blocks=12, seed=17, width=192, image_size=224)
        place(model, "CPU", dtypes.half)
        place(model, "CPU", dtypes.float)
        x = (np.random.default_rng(123).standard_normal((1, 3, 224, 224)) * 0.1).astype(
            np.float16
        )
        logits = model(Tensor(x.astype(np.float32), device="CPU")).numpy()
    dest = ROOT / ".kt/launch-evidence"
    dest.mkdir(parents=True, exist_ok=True)
    np.savez(dest / "vit-b1-cpu.npz", **{"step0/logits": logits})
    print(json.dumps(dict(shape=logits.shape, finite=bool(np.isfinite(logits).all()))))


if __name__ == "__main__":
    main()
