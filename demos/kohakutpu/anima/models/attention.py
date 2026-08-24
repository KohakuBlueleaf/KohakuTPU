"""Multi-Head Self-Attention and Cross-Attention modules for Anima DiT on KohakuTPU."""

import numpy as np
from tinygrad import Tensor, dtypes
from tinygrad.device import Device

from kohakutpu import kernels as K

from .layers import Linear, RMSNorm
from .rope import apply_rotary_emb


def _run_flash_attention(
    dev, qh: np.ndarray, kh: np.ndarray, vh: np.ndarray, block: int
) -> np.ndarray:
    """Execute K.flash_attention on KTPU and immediately release intermediate buffers."""
    at = qh.shape[-1] ** -0.5
    scaled = (qh.astype(np.float32) * at * 1.4426950408889634).astype(np.float16)
    flat_v = np.ascontiguousarray(np.swapaxes(vh, -1, -2))

    q_arr = dev.tensor(scaled)
    k_arr = dev.tensor(kh)
    v_arr = dev.tensor(flat_v)

    attn_arr = K.flash_attention(q_arr, k_arr, v_arr, block=block)
    attn_np = attn_arr.numpy().copy()

    q_arr.release()
    k_arr.release()
    v_arr.release()
    attn_arr.release()
    return attn_np


class SelfAttention:
    """Batched Multi-Head Self-Attention with QK-RMSNorm and 3D/2D RoPE on KohakuTPU."""

    def __init__(
        self,
        query_dim: int,
        num_heads: int = 16,
        head_dim: int = 128,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.query_dim = query_dim
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.inner_dim = num_heads * head_dim
        self.device = device
        self.dtype = dtype

        self.q_proj = Linear(
            query_dim, self.inner_dim, bias=False, device=device, dtype=dtype
        )
        self.k_proj = Linear(
            query_dim, self.inner_dim, bias=False, device=device, dtype=dtype
        )
        self.v_proj = Linear(
            query_dim, self.inner_dim, bias=False, device=device, dtype=dtype
        )

        self.q_norm = RMSNorm(head_dim, eps=1e-6, device=device, dtype=dtype)
        self.k_norm = RMSNorm(head_dim, eps=1e-6, device=device, dtype=dtype)

        self.output_proj = Linear(
            self.inner_dim, query_dim, bias=False, device=device, dtype=dtype
        )

    def __call__(
        self,
        x: Tensor,
        rope_cos: Tensor | None = None,
        rope_sin: Tensor | None = None,
        rope_P: Tensor | None = None,
    ) -> Tensor:
        """
        x: (B, L, query_dim)
        rope_cos, rope_sin: (1, L, 1, head_dim)
        rope_P: (head_dim, head_dim)
        Returns: (B, L, query_dim)
        """
        B, L, _ = x.shape
        H = self.num_heads
        D = self.head_dim

        # 1. Linear projections on KTPU
        q = self.q_proj(x).reshape(B, L, H, D).realize()
        k = self.k_proj(x).reshape(B, L, H, D).realize()
        v = self.v_proj(x).reshape(B, L, H, D).realize()

        # 2. QK-RMSNorm on KTPU
        q = self.q_norm(q).realize()
        k = self.k_norm(k).realize()

        # 3. Rotary Positional Embedding
        if rope_cos is not None and rope_sin is not None and rope_P is not None:
            q = apply_rotary_emb(q, rope_cos, rope_sin, rope_P)
            k = apply_rotary_emb(k, rope_cos, rope_sin, rope_P)

        # 4. Multi-Head Flash Attention on KTPU
        dev = Device[self.device].rt
        qh = np.ascontiguousarray(q.numpy().transpose(0, 2, 1, 3))
        kh = np.ascontiguousarray(k.numpy().transpose(0, 2, 1, 3))
        vh = np.ascontiguousarray(v.numpy().transpose(0, 2, 1, 3))

        attn_out_np = _run_flash_attention(dev, qh, kh, vh, block=D)

        # 5. Output projection on KTPU
        attn_flat_np = np.ascontiguousarray(
            attn_out_np.transpose(0, 2, 1, 3).reshape(B, L, self.inner_dim)
        )
        attn_flat = Tensor(attn_flat_np, device=self.device, dtype=self.dtype)
        out = self.output_proj(attn_flat)
        return out


class CrossAttention:
    """Batched Multi-Head Cross-Attention with text conditioning on KohakuTPU."""

    def __init__(
        self,
        query_dim: int,
        context_dim: int,
        num_heads: int = 16,
        head_dim: int = 128,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.query_dim = query_dim
        self.context_dim = context_dim
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.inner_dim = num_heads * head_dim
        self.device = device
        self.dtype = dtype

        self.q_proj = Linear(
            query_dim, self.inner_dim, bias=False, device=device, dtype=dtype
        )
        self.k_proj = Linear(
            context_dim, self.inner_dim, bias=False, device=device, dtype=dtype
        )
        self.v_proj = Linear(
            context_dim, self.inner_dim, bias=False, device=device, dtype=dtype
        )

        self.q_norm = RMSNorm(head_dim, eps=1e-6, device=device, dtype=dtype)
        self.k_norm = RMSNorm(head_dim, eps=1e-6, device=device, dtype=dtype)

        self.output_proj = Linear(
            self.inner_dim, query_dim, bias=False, device=device, dtype=dtype
        )

    def __call__(
        self,
        x: Tensor,
        context: Tensor,
    ) -> Tensor:
        """
        x: (B, Lq, query_dim)
        context: (B, Lkv, context_dim)
        Returns: (B, Lq, query_dim)
        """
        B, Lq, _ = x.shape
        _, Lkv, _ = context.shape
        H = self.num_heads
        D = self.head_dim

        # 1. Projections on KTPU
        q = self.q_proj(x).reshape(B, Lq, H, D).realize()
        k = self.k_proj(context).reshape(B, Lkv, H, D).realize()
        v = self.v_proj(context).reshape(B, Lkv, H, D).realize()

        # 2. QK-RMSNorm on KTPU
        q = self.q_norm(q).realize()
        k = self.k_norm(k).realize()

        # 3. Transpose heads and run Flash Attention on KTPU
        dev = Device[self.device].rt
        qh = np.ascontiguousarray(q.numpy().transpose(0, 2, 1, 3))
        kh = np.ascontiguousarray(k.numpy().transpose(0, 2, 1, 3))
        vh = np.ascontiguousarray(v.numpy().transpose(0, 2, 1, 3))

        attn_out_np = _run_flash_attention(dev, qh, kh, vh, block=D)

        # 4. Output projection on KTPU
        attn_flat_np = np.ascontiguousarray(
            attn_out_np.transpose(0, 2, 1, 3).reshape(B, Lq, self.inner_dim)
        )
        attn_flat = Tensor(attn_flat_np, device=self.device, dtype=self.dtype)
        out = self.output_proj(attn_flat)
        return out
