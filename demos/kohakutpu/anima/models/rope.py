"""2D Spatial Rotary Positional Embedding (RoPE) for Anima Text-to-Image DiT on KohakuTPU."""

import numpy as np
from tinygrad import Tensor, dtypes


class SpatialRopeEmbedding2D:
    """2D Spatial Rotary Positional Embedding for (Height, Width) on KohakuTPU."""

    def __init__(
        self,
        head_dim: int = 128,
        h_extrapolation_ratio: float = 4.0,
        w_extrapolation_ratio: float = 4.0,
        rope_theta: float = 10000.0,
    ):
        self.head_dim = head_dim
        self.rope_theta = rope_theta

        # Split head_dim equally between Height and Width
        self.dim_h = head_dim // 2
        self.dim_w = head_dim - self.dim_h
        assert (
            self.dim_h + self.dim_w == head_dim
        ), f"Invalid dim split: {self.dim_h} + {self.dim_w} != {head_dim}"

        self.h_ntk_factor = h_extrapolation_ratio ** (
            self.dim_h / max(self.dim_h - 2, 1)
        )
        self.w_ntk_factor = w_extrapolation_ratio ** (
            self.dim_w / max(self.dim_w - 2, 1)
        )

        self.h_theta = rope_theta * self.h_ntk_factor
        self.w_theta = rope_theta * self.w_ntk_factor

        self.dim_spatial_range_h = (
            np.arange(0, self.dim_h, 2, dtype=np.float32) / self.dim_h
        )
        self.dim_spatial_range_w = (
            np.arange(0, self.dim_w, 2, dtype=np.float32) / self.dim_w
        )

        self.h_spatial_freqs = 1.0 / (self.h_theta**self.dim_spatial_range_h)
        self.w_spatial_freqs = 1.0 / (self.w_theta**self.dim_spatial_range_w)

    def get_rotary_cos_sin(
        self,
        grid_size: tuple[int, int],
        device: str = "KTPU",
        dtype=dtypes.float16,
    ) -> tuple[Tensor, Tensor, Tensor]:
        """
        Compute (cos, sin, P) tables for 2D spatial grid (H, W).
        Returns:
            cos: (1, L, 1, head_dim)
            sin: (1, L, 1, head_dim)
            P: (head_dim, head_dim) pair rotation matrix
        """
        H, W = grid_size

        seq_h = np.arange(H, dtype=np.float32)
        seq_w = np.arange(W, dtype=np.float32)

        half_emb_h = np.outer(seq_h, self.h_spatial_freqs)
        half_emb_w = np.outer(seq_w, self.w_spatial_freqs)

        emb_h = np.repeat(half_emb_h, 2, axis=-1)  # (H, dim_h)
        emb_w = np.repeat(half_emb_w, 2, axis=-1)  # (W, dim_w)

        # Broadcast across 2D grid: (H, W, dim_h) and (H, W, dim_w)
        emb_h_2d = np.broadcast_to(emb_h[:, None, :], (H, W, self.dim_h))
        emb_w_2d = np.broadcast_to(emb_w[None, :, :], (H, W, self.dim_w))

        # Concatenate along channel dimension: (H, W, head_dim)
        freqs_2d = np.concatenate([emb_h_2d, emb_w_2d], axis=-1)
        freqs_flat = freqs_2d.reshape(H * W, self.head_dim).astype(np.float16)

        cos_np = np.cos(freqs_flat)[None, :, None, :]  # (1, L, 1, head_dim)
        sin_np = np.sin(freqs_flat)[None, :, None, :]  # (1, L, 1, head_dim)

        # Pair rotation matrix: [[0, 1], [-1, 0]] blocks
        P_np = np.zeros((self.head_dim, self.head_dim), dtype=np.float16)
        for k in range(self.head_dim // 2):
            P_np[2 * k, 2 * k + 1] = 1.0
            P_np[2 * k + 1, 2 * k] = -1.0

        cos_tensor = Tensor(cos_np, device=device, dtype=dtype)
        sin_tensor = Tensor(sin_np, device=device, dtype=dtype)
        P_tensor = Tensor(P_np, device=device, dtype=dtype)
        return cos_tensor, sin_tensor, P_tensor


def apply_rotary_emb(x: Tensor, cos: Tensor, sin: Tensor, P: Tensor) -> Tensor:
    """
    Applies 2D rotary position embedding to query or key tensor on KohakuTPU.
    x: (B, L, H, head_dim)
    cos, sin: (1, L, 1, head_dim)
    P: (head_dim, head_dim)
    """
    B, L, H, D = x.shape
    x_cos = (x * cos).realize()
    x_swap = (x.reshape(-1, D) @ P).reshape(B, L, H, D).realize()
    x_sin = (x_swap * sin).realize()
    x_rot = (x_cos + x_sin).realize()
    return x_rot
