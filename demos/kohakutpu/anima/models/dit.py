"""Anima Diffusion Transformer (DiT) architecture on KohakuTPU."""

from typing import Any

from tinygrad import Tensor, dtypes

from ..config import AnimaConfig
from .blocks import AnimaBlock
from .layers import FinalLayer, PatchEmbed, TimestepEmbedder
from .rope import SpatialRopeEmbedding2D


class AnimaDiT:
    """Anima Diffusion Transformer Backbone for Text-to-Image on KohakuTPU."""

    def __init__(self, config: AnimaConfig | None = None):
        if config is None:
            config = AnimaConfig.base_2b()
        self.config = config
        self.device = config.device
        self.dtype = getattr(dtypes, config.dtype)

        # 1. 2D Patch Embedding: (B, 16, H, W) -> (B, (H/2)*(W/2), model_channels)
        self.patch_embed = PatchEmbed(
            in_channels=config.in_channels,
            patch_spatial=config.patch_spatial,
            hidden_size=config.model_channels,
            device=self.device,
            dtype=self.dtype,
        )

        # 2. Timestep Embedding
        self.t_embedder = TimestepEmbedder(
            hidden_size=config.model_channels,
            frequency_embedding_size=256,
            device=self.device,
            dtype=self.dtype,
        )

        # 3. 2D Spatial Rotary Positional Embedding (RoPE)
        self.pos_emb = SpatialRopeEmbedding2D(
            head_dim=config.head_dim,
            h_extrapolation_ratio=config.rope_h_extrapolation_ratio,
            w_extrapolation_ratio=config.rope_w_extrapolation_ratio,
            rope_theta=config.rope_theta,
        )

        # 4. Transformer Blocks
        self.blocks = [
            AnimaBlock(
                x_dim=config.model_channels,
                context_dim=config.crossattn_emb_channels,
                num_heads=config.num_heads,
                mlp_ratio=config.mlp_ratio,
                use_adaln_lora=config.use_adaln_lora,
                adaln_lora_dim=config.adaln_lora_dim,
                device=self.device,
                dtype=self.dtype,
            )
            for _ in range(config.num_blocks)
        ]

        # 5. Final Output Layer: tokens -> latents (B, 16, H, W)
        self.final_layer = FinalLayer(
            hidden_size=config.model_channels,
            patch_spatial=config.patch_spatial,
            out_channels=config.out_channels,
            use_adaln_lora=config.use_adaln_lora,
            adaln_lora_dim=config.adaln_lora_dim,
            device=self.device,
            dtype=self.dtype,
        )

    def __call__(
        self,
        x: Tensor,
        timesteps: Tensor,
        context: Tensor,
    ) -> Tensor:
        """
        Forward pass of Anima DiT.

        Args:
            x: Input 2D latents (B, C=16, H, W).
            timesteps: Timestep values (B,).
            context: Text conditioning features (B, L_context, crossattn_dim).

        Returns:
            Predicted velocity / noise tensor matching input shape (B, 16, H, W).
        """
        B, C, H, W = x.shape

        # 1. Patchify input
        x_tokens, grid_size = self.patch_embed(x)

        # 2. Timestep embedding
        t_emb = self.t_embedder(timesteps)

        # 3. 2D RoPE frequency tables
        rope_cos, rope_sin, rope_P = self.pos_emb.get_rotary_cos_sin(
            grid_size, device=self.device, dtype=self.dtype
        )

        # 4. Sequential transformer blocks
        h = x_tokens
        for block in self.blocks:
            h = block(
                h,
                emb=t_emb,
                context=context,
                rope_cos=rope_cos,
                rope_sin=rope_sin,
                rope_P=rope_P,
            )

        # 5. Final projection & unpatchify
        out = self.final_layer(h, emb=t_emb, grid_size=grid_size)

        return out

    def state_dict(self) -> dict[str, Any]:
        from tinygrad.nn.state import get_state_dict

        return get_state_dict(self)

    def load_state_dict(self, state_dict: dict[str, Any], strict: bool = False):
        from tinygrad.nn.state import load_state_dict

        load_state_dict(self, state_dict, strict=strict)
