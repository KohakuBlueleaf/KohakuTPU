"""FeedForward / MLP module for Anima DiT using KohakuTPU compiler's internal kernel library."""

import math

import numpy as np
from ktpugrad import bridge
from tinygrad import Tensor, dtypes

from kohakutpu import kernels as K

# Bridge the compiler's internal fused two-projection MLP kernel
_ktpu_mlp = bridge(K.mlp)


class FeedForward:
    """
    FeedForward network: down(gelu(up(x)))
    Executed as KohakuTPU's fused resident-tile MLP kernel.
    """

    def __init__(
        self,
        d_model: int,
        d_ff: int,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.d_model = d_model
        self.d_ff = d_ff
        self.device = device
        self.dtype = dtype

        # Initialize weights on host in float16, upload to KTPU
        bound_up = 1.0 / math.sqrt(d_model) if d_model > 0 else 0.01
        bound_down = 1.0 / math.sqrt(d_ff) if d_ff > 0 else 0.01

        w_up_np = np.random.uniform(-bound_up, bound_up, (d_ff, d_model)).astype(
            np.float16
        )
        w_down_np = np.random.uniform(-bound_down, bound_down, (d_model, d_ff)).astype(
            np.float16
        )

        self.w_up = Tensor(w_up_np, device=device, dtype=dtype)
        self.w_down = Tensor(w_down_np, device=device, dtype=dtype)

    def __call__(self, x: Tensor) -> Tensor:
        orig_shape = x.shape
        x_2d = x.reshape(-1, self.d_model)

        # Execute KTPU fused MLP kernel with GELU activation on resident accumulator tile
        out_2d = _ktpu_mlp(x_2d, self.w_up, self.w_down, act="gelu").realize()

        if len(orig_shape) > 2:
            return out_2d.reshape(*orig_shape[:-1], self.d_model)
        return out_2d
