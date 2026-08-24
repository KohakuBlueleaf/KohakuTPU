"""LLMAdapter module for mapping text representations to DiT context space on KohakuTPU."""

import numpy as np
from tinygrad import Tensor, dtypes

from .layers import Linear, RMSNorm


class LLMAdapter:
    """Projects text representation into DiT context space."""

    def __init__(
        self,
        source_dim: int = 1024,
        target_dim: int = 1024,
        max_seq_len: int = 512,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.source_dim = source_dim
        self.target_dim = target_dim
        self.max_seq_len = max_seq_len
        self.device = device
        self.dtype = dtype

        self.in_proj = Linear(
            source_dim, target_dim, bias=False, device=device, dtype=dtype
        )
        self.norm = RMSNorm(target_dim, eps=1e-6, device=device, dtype=dtype)
        self.out_proj = Linear(
            target_dim, target_dim, bias=False, device=device, dtype=dtype
        )

    def __call__(self, text_embeds: Tensor) -> Tensor:
        h = self.norm(self.in_proj(text_embeds))
        out = self.out_proj(h)

        B, L, D = out.shape
        if L != self.max_seq_len:
            out_np = out.numpy()
            if L < self.max_seq_len:
                padded = np.pad(out_np, ((0, 0), (0, self.max_seq_len - L), (0, 0)))
            else:
                padded = out_np[:, : self.max_seq_len, :]
            out = Tensor(padded, device=self.device, dtype=self.dtype)

        return out
