"""Configuration definitions for the Anima Diffusion Transformer architecture."""

from dataclasses import dataclass


@dataclass
class AnimaConfig:
    """Anima DiT model configuration parameters."""

    # Latent channels
    in_channels: int = 16
    out_channels: int = 16

    # Patching
    patch_spatial: int = 2
    patch_temporal: int = 1

    # Transformer dimensions
    model_channels: int = 2048
    num_blocks: int = 28
    num_heads: int = 16
    head_dim: int = 128  # model_channels // num_heads
    mlp_ratio: float = 4.0  # MLP hidden dim = model_channels * mlp_ratio

    # Cross attention conditioning
    crossattn_emb_channels: int = 1024

    # AdaLN-LoRA modulation
    use_adaln_lora: bool = True
    adaln_lora_dim: int = 256

    # Rotary Positional Embedding (3D RoPE)
    pos_emb_cls: str = "rope3d"
    base_fps: int = 24
    rope_theta: float = 10000.0
    rope_h_extrapolation_ratio: float = 4.0
    rope_w_extrapolation_ratio: float = 4.0
    rope_t_extrapolation_ratio: float = 1.0
    rope_enable_fps_modulation: bool = False

    # Image bounds
    max_img_h: int = 240
    max_img_w: int = 240
    max_frames: int = 128

    # Normalization epsilon
    norm_eps: float = 1e-6

    # Device & precision defaults
    device: str = "KTPU"
    dtype: str = "float16"

    @classmethod
    def base_2b(cls, **overrides) -> "AnimaConfig":
        """Default 2.0B parameter Anima DiT configuration."""
        cfg = cls(
            model_channels=2048,
            num_blocks=28,
            num_heads=16,
            head_dim=128,
            crossattn_emb_channels=1024,
            use_adaln_lora=True,
            adaln_lora_dim=256,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    @classmethod
    def large_2_9b(cls, **overrides) -> "AnimaConfig":
        """2.9B parameter Anima DiT configuration (40 blocks)."""
        cfg = cls(
            model_channels=2048,
            num_blocks=40,
            num_heads=16,
            head_dim=128,
            crossattn_emb_channels=1024,
            use_adaln_lora=True,
            adaln_lora_dim=256,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    @classmethod
    def tiny(cls, **overrides) -> "AnimaConfig":
        """Lightweight configuration for rapid simulation and unit testing."""
        cfg = cls(
            model_channels=256,
            num_blocks=2,
            num_heads=4,
            head_dim=64,
            crossattn_emb_channels=128,
            use_adaln_lora=True,
            adaln_lora_dim=64,
        )
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg
