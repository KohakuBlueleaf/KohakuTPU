"""16-channel Autoencoder (VAE) Decoder for Anima DiT on KohakuTPU."""

import numpy as np
from tinygrad import Tensor, dtypes

from .layers import Linear


class SimpleVAEDecoder:
    """
    16-channel Latent to RGB Decoder with 8x spatial upsampling.
    Maps latents (B, 16, H, W) -> (B, 3, 8H, 8W) pixels.
    """

    def __init__(
        self,
        in_channels: int = 16,
        out_channels: int = 3,
        hidden_channels: int = 64,
        scale_factor: float = 0.3611,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.hidden_channels = hidden_channels
        self.scale_factor = scale_factor
        self.device = device
        self.dtype = dtype

        self.proj = Linear(
            in_channels, out_channels * 8 * 8, bias=True, device=device, dtype=dtype
        )

    def decode(self, latents: Tensor) -> Tensor:
        """
        latents: (B, 16, H, W) or (B, 16, 1, H, W)
        Returns: (B, 3, 8H, 8W) RGB tensor in [0, 1] range.
        """
        if len(latents.shape) == 5:
            latents = latents.squeeze(2)

        unscaled = (latents * (1.0 / self.scale_factor)).realize()

        B, C, H, W = unscaled.shape
        unscaled_np = unscaled.numpy()
        lat_perm_np = np.ascontiguousarray(unscaled_np.transpose(0, 2, 3, 1))
        lat_perm = Tensor(lat_perm_np, device=self.device, dtype=self.dtype)
        subpixels = self.proj(lat_perm).realize()

        sub_np = subpixels.numpy()
        sub_reshaped = np.ascontiguousarray(
            sub_np.reshape(B, H, W, self.out_channels, 8, 8)
            .transpose(0, 3, 1, 4, 2, 5)
            .reshape(B, self.out_channels, H * 8, W * 8)
        )
        rgb = Tensor(sub_reshaped, device=self.device, dtype=self.dtype)
        return rgb.sigmoid().realize()
