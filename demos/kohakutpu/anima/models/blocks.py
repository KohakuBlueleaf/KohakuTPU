"""Anima Transformer Block combining Self-Attention, Cross-Attention, and MLP on KohakuTPU."""

from tinygrad import Tensor, dtypes

from .attention import CrossAttention, SelfAttention
from .feedforward import FeedForward
from .layers import AdaLNLoRAModulation, LayerNorm


class AnimaBlock:
    """Anima DiT Transformer Block with AdaLN modulation."""

    def __init__(
        self,
        x_dim: int = 2048,
        context_dim: int = 1024,
        num_heads: int = 16,
        mlp_ratio: float = 4.0,
        use_adaln_lora: bool = True,
        adaln_lora_dim: int = 256,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.x_dim = x_dim
        self.context_dim = context_dim
        self.num_heads = num_heads
        self.head_dim = x_dim // num_heads
        self.device = device
        self.dtype = dtype

        # 1. Self-Attention
        self.norm_self_attn = LayerNorm(
            x_dim, elementwise_affine=False, eps=1e-6, device=device, dtype=dtype
        )
        self.self_attn = SelfAttention(
            query_dim=x_dim,
            num_heads=num_heads,
            head_dim=self.head_dim,
            device=device,
            dtype=dtype,
        )
        self.adaln_self_attn = AdaLNLoRAModulation(
            x_dim,
            n_chunks=3,
            lora_dim=adaln_lora_dim,
            use_lora=use_adaln_lora,
            device=device,
            dtype=dtype,
        )

        # 2. Cross-Attention
        self.norm_cross_attn = LayerNorm(
            x_dim, elementwise_affine=False, eps=1e-6, device=device, dtype=dtype
        )
        self.cross_attn = CrossAttention(
            query_dim=x_dim,
            context_dim=context_dim,
            num_heads=num_heads,
            head_dim=self.head_dim,
            device=device,
            dtype=dtype,
        )
        self.adaln_cross_attn = AdaLNLoRAModulation(
            x_dim,
            n_chunks=3,
            lora_dim=adaln_lora_dim,
            use_lora=use_adaln_lora,
            device=device,
            dtype=dtype,
        )

        # 3. FeedForward (fused MLP kernel)
        self.norm_mlp = LayerNorm(
            x_dim, elementwise_affine=False, eps=1e-6, device=device, dtype=dtype
        )
        self.mlp = FeedForward(
            d_model=x_dim,
            d_ff=int(x_dim * mlp_ratio),
            device=device,
            dtype=dtype,
        )
        self.adaln_mlp = AdaLNLoRAModulation(
            x_dim,
            n_chunks=3,
            lora_dim=adaln_lora_dim,
            use_lora=use_adaln_lora,
            device=device,
            dtype=dtype,
        )

    def __call__(
        self,
        x: Tensor,
        emb: Tensor,
        context: Tensor,
        rope_cos: Tensor | None = None,
        rope_sin: Tensor | None = None,
        rope_P: Tensor | None = None,
    ) -> Tensor:
        """
        x: (B, L, x_dim)
        emb: (B, 1, x_dim) or (B, x_dim)
        context: (B, L_ctx, context_dim)
        Returns: (B, L, x_dim)
        """
        if len(emb.shape) == 2:
            emb = emb.unsqueeze(1)

        # 1. Self-Attention with AdaLN
        shift_sa, scale_sa, gate_sa = self.adaln_self_attn(emb)
        normed_sa = (
            self.norm_self_attn(x) * (1.0 + scale_sa).realize() + shift_sa
        ).realize()
        attn_out = self.self_attn(
            normed_sa, rope_cos=rope_cos, rope_sin=rope_sin, rope_P=rope_P
        )
        x = (x + gate_sa * attn_out).realize()

        # 2. Cross-Attention with AdaLN
        shift_ca, scale_ca, gate_ca = self.adaln_cross_attn(emb)
        normed_ca = (
            self.norm_cross_attn(x) * (1.0 + scale_ca).realize() + shift_ca
        ).realize()
        cross_out = self.cross_attn(normed_ca, context=context)
        x = (x + gate_ca * cross_out).realize()

        # 3. FeedForward with AdaLN
        shift_mlp, scale_mlp, gate_mlp = self.adaln_mlp(emb)
        normed_mlp = (
            self.norm_mlp(x) * (1.0 + scale_mlp).realize() + shift_mlp
        ).realize()
        mlp_out = self.mlp(normed_mlp)
        x = (x + gate_mlp * mlp_out).realize()

        return x
