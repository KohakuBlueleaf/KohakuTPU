"""Using KohakuTPU from tinygrad: write a model, name the device, run it.

READ THIS FILE. If you came from tinygrad there is one new line -- the
`install()` below -- and after that you write the tinygrad you already write.

The model is deliberately in the ordinary shape: layers as attributes, a
`__call__`, weights moved with `nn.state`. Nothing here is written for this
backend, which is the point.
"""

import ktpugrad
import numpy as np
from tinygrad import nn
from tinygrad.dtype import dtypes
from tinygrad.nn.state import get_parameters
from tinygrad.tensor import Tensor

# The whole of the setup: registers the device with tinygrad and routes
# lowering through the kernel matcher.
ktpugrad.install()
DEV = ktpugrad.NAME


class FeedForward:
    """A transformer's feed forward, in the shape tinygrad models are written.

    prefer `Linear(bias=False)` because a bias is a second operand and this backend
    would rather fuse the activation onto the contraction than carry one; see
    `02_kernel.py` for what that costs when you do want it.
    """

    def __init__(self, dim: int, hidden: int) -> None:
        self.up = nn.Linear(dim, hidden, bias=True)
        self.down = nn.Linear(hidden, dim, bias=False)

    def __call__(self, x: Tensor) -> Tensor:
        # `.silu()` on a matmul is the fused case: one kernel, and the
        # intermediate never returns through MAG. You did not ask for that.
        return self.down(self.up(x).silu())


def main() -> None:
    # Init stays on the host: `nn.Linear` seeds itself with an RNG, and this
    # card carries fp16 only -- integer arithmetic has no home on it.
    Tensor.manual_seed(0)
    model = FeedForward(1024, 2048)

    # Then move, which is the same two lines as moving to any other accelerator.
    for p in get_parameters(model):
        p.replace(p.cast(dtypes.half).to(DEV)).realize()

    x = Tensor(
        np.asarray(np.random.default_rng(0).normal(0, 1, (64, 1024)), np.float16),
        device=DEV,
    )
    y = np.asarray(model(x).numpy(), np.float64)

    xf = np.asarray(x.numpy(), np.float64)
    u = xf @ np.asarray(model.up.weight.numpy(), np.float64).T
    want = (u / (1.0 + np.exp(-u))) @ np.asarray(
        model.down.weight.numpy(), np.float64
    ).T

    err = np.abs(y - want).max() / (np.abs(want).max() or 1.0)
    print("feedforward", "ok" if err < 0.1 else f"FAILED ({err:.3e})")
    print("shape      ", "ok" if y.shape == (64, 1024) else f"FAILED {y.shape}")


if __name__ == "__main__":
    main()
