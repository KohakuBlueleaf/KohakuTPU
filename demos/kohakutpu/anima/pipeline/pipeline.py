"""End-to-end Text-to-Image inference pipeline for Anima on KohakuTPU."""

import gc
import time

import ktpugrad
import numpy as np
from tinygrad import Tensor, dtypes

from ..config import AnimaConfig
from ..models.adapter import LLMAdapter
from ..models.dit import AnimaDiT
from ..models.vae import SimpleVAEDecoder
from .image import RGBImage
from .scheduler import FlowMatchScheduler
from .text_encoder import SimpleTextEncoder


class AnimaPipeline:
    """Text-to-Image Diffusion Pipeline for Anima on KohakuTPU."""

    def __init__(
        self,
        dit: AnimaDiT | None = None,
        scheduler: FlowMatchScheduler | None = None,
        text_encoder: SimpleTextEncoder | None = None,
        llm_adapter: LLMAdapter | None = None,
        vae: SimpleVAEDecoder | None = None,
        config: AnimaConfig | None = None,
    ):
        if config is None:
            config = AnimaConfig.tiny() if dit is None else dit.config
        self.config = config
        self.device = config.device
        self.dtype = getattr(dtypes, config.dtype)

        ktpugrad.install()
        # Expand device arena for multi-step diffusion pipeline
        if self.device == "KTPU":
            from tinygrad.device import Device

            if hasattr(Device["KTPU"], "rt") and hasattr(Device["KTPU"].rt, "arena"):
                Device["KTPU"].rt.arena.size = max(
                    Device["KTPU"].rt.arena.size, 256 << 20
                )

        self.dit = dit if dit is not None else AnimaDiT(config)
        self.scheduler = scheduler if scheduler is not None else FlowMatchScheduler()
        self.text_encoder = (
            text_encoder
            if text_encoder is not None
            else SimpleTextEncoder(
                embed_dim=config.crossattn_emb_channels,
                device=self.device,
                dtype=self.dtype,
            )
        )
        self.llm_adapter = llm_adapter
        self.vae = (
            vae
            if vae is not None
            else SimpleVAEDecoder(
                in_channels=config.out_channels,
                device=self.device,
                dtype=self.dtype,
            )
        )

    @classmethod
    def from_config(cls, config: AnimaConfig) -> "AnimaPipeline":
        return cls(config=config)

    def encode_prompt(
        self, prompt: str, negative_prompt: str = ""
    ) -> tuple[Tensor, Tensor]:
        pos_emb = self.text_encoder.encode_text(prompt)
        neg_emb = self.text_encoder.encode_text(negative_prompt)

        if self.llm_adapter is not None:
            pos_emb = self.llm_adapter(pos_emb)
            neg_emb = self.llm_adapter(neg_emb)

        return pos_emb, neg_emb

    def __call__(
        self,
        prompt: str,
        negative_prompt: str = "",
        height: int = 256,
        width: int = 256,
        num_inference_steps: int = 10,
        cfg_scale: float = 4.0,
        seed: int | None = None,
        verbose: bool = True,
    ) -> RGBImage:
        assert (
            height % 16 == 0 and width % 16 == 0
        ), f"Dimensions must be divisible by 16: got {height}x{width}"

        if seed is not None:
            np.random.seed(seed)

        # 1. Initialize latent noise: (B, C, H_latent, W_latent) where H_latent = H / 8
        lat_h = height // 8
        lat_w = width // 8
        noise_np = np.random.randn(1, self.config.in_channels, lat_h, lat_w).astype(
            np.float16
        )
        latents = Tensor(noise_np, device=self.device, dtype=self.dtype)

        # 2. Encode text prompts
        pos_context, neg_context = self.encode_prompt(prompt, negative_prompt)

        # 3. Configure scheduler timesteps
        self.scheduler.set_timesteps(num_inference_steps)

        if verbose:
            print(
                f"[AnimaPipeline] Sampling on {self.device}: {num_inference_steps} steps, {height}x{width} resolution"
            )

        # 4. Denoising loop on KohakuTPU
        start_time = time.time()
        for i, t_val in enumerate(self.scheduler.timesteps[:-1]):
            step_start = time.time()
            t_tensor = Tensor(
                np.array([t_val], dtype=np.float16),
                device=self.device,
                dtype=self.dtype,
            )

            # Positive velocity prediction on KTPU
            v_cond = self.dit(latents, t_tensor, pos_context)

            # CFG combination
            if cfg_scale > 1.0:
                v_uncond = self.dit(latents, t_tensor, neg_context)
                v_pred = (v_uncond + (v_cond - v_uncond) * cfg_scale).realize()
            else:
                v_pred = v_cond.realize()

            # Euler ODE step on KTPU
            latents = self.scheduler.step(v_pred, i, latents)
            latents_np = latents.numpy()
            del v_cond, v_pred, t_tensor
            if cfg_scale > 1.0:
                del v_uncond
            gc.collect()

            latents = Tensor(latents_np, device=self.device, dtype=self.dtype)

            if verbose:
                dt_ms = (time.time() - step_start) * 1000
                print(
                    f"  Step {i + 1:2d}/{num_inference_steps} (t={t_val:6.1f}) [{dt_ms:6.1f} ms]"
                )

        total_time = time.time() - start_time
        if verbose:
            print(f"[AnimaPipeline] Denoising complete in {total_time:.2f}s")

        # 5. Decode latents to RGB image on KTPU
        rgb_tensor = self.vae.decode(latents)
        rgb_np = rgb_tensor.numpy()[0]  # (3, H, W)

        rgb_clamped = np.clip(rgb_np * 255.0, 0, 255).astype(np.uint8)
        img = RGBImage(rgb_clamped)

        return img
