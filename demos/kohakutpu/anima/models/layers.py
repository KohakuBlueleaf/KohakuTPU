"""Neural network layer primitives for Anima 2D Text-to-Image DiT on KohakuTPU."""

import math

import numpy as np
from kohakutpu.kernels import layernorm_wide, rmsnorm_wide, split
from ktpugrad import layernorm as ktpu_layernorm
from ktpugrad import rmsnorm as ktpu_rmsnorm
from tinygrad import Tensor, dtypes
from tinygrad.device import Device


class Linear:
    """Linear projection layer: Y = X @ W.T + B on KohakuTPU tensor cores."""

    def __init__(
        self,
        in_features: int,
        out_features: int,
        bias: bool = True,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.in_features = in_features
        self.out_features = out_features
        self.device = device
        self.dtype = dtype

        bound = 1.0 / math.sqrt(in_features) if in_features > 0 else 0.01
        w_np = np.random.uniform(-bound, bound, (out_features, in_features)).astype(
            np.float16
        )
        self.weight = Tensor(w_np, device=device, dtype=dtype)

        if bias:
            b_np = np.zeros((out_features,), dtype=np.float16)
            self.bias: Tensor | None = Tensor(b_np, device=device, dtype=dtype)
        else:
            self.bias = None

    def __call__(self, x: Tensor) -> Tensor:
        orig_shape = x.shape
        x_2d = x.reshape(-1, self.in_features)
        M = x_2d.shape[0]

        # KTPU matrix cluster expects M >= 4 (tensor core minimum tile granularity)
        if M < 4:
            x_np = x_2d.numpy()
            padded_np = np.zeros((4, self.in_features), dtype=np.float16)
            padded_np[:M] = x_np
            x_padded = Tensor(padded_np, device=self.device, dtype=self.dtype)
            matmul_out = (x_padded @ self.weight.T).realize()
            out_2d = Tensor(
                matmul_out.numpy()[:M], device=self.device, dtype=self.dtype
            )
        else:
            out_2d = (x_2d @ self.weight.T).realize()

        if self.bias is not None:
            out_2d = (out_2d + self.bias).realize()

        if len(orig_shape) > 2:
            return out_2d.reshape(*orig_shape[:-1], self.out_features)
        return out_2d


class RMSNorm:
    """Root Mean Square Layer Normalization using KohakuTPU compiler's rmsnorm kernels."""

    def __init__(
        self,
        dim: int,
        eps: float = 1e-6,
        elementwise_affine: bool = True,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.dim = dim
        self.eps = eps
        self.elementwise_affine = elementwise_affine
        self.device = device
        self.dtype = dtype

        if elementwise_affine:
            w_np = np.ones((dim,), dtype=np.float16)
            self.weight: Tensor | None = Tensor(w_np, device=device, dtype=dtype)
        else:
            self.weight = None

    def __call__(self, x: Tensor) -> Tensor:
        orig_shape = x.shape
        x_2d = x.reshape(-1, self.dim)
        M, D = x_2d.shape
        w = (
            self.weight
            if self.weight is not None
            else Tensor(np.ones((self.dim,), dtype=np.float16), device=self.device)
        )

        if D <= 128:
            out_2d = ktpu_rmsnorm(x_2d, w, eps=self.eps).realize()
        else:
            # Hierarchical wide-row RMSNorm on KohakuTPU vector core
            dev = Device[self.device].rt
            rows = split(D)
            sub_cols = D // rows
            x_np = x_2d.numpy()
            w_np = w.numpy()
            x_split = dev.tensor(x_np.reshape(M, rows, sub_cols))
            w_split = dev.tensor(
                np.broadcast_to(w_np.reshape(1, rows, sub_cols), (M, rows, sub_cols))
            )
            out_split = rmsnorm_wide(x_split, w_split, rows=rows)
            out_2d = Tensor(
                out_split.numpy().reshape(M, D), device=self.device, dtype=self.dtype
            )
            x_split.release()
            w_split.release()
            out_split.release()

        if len(orig_shape) > 2:
            return out_2d.reshape(*orig_shape)
        return out_2d


class LayerNorm:
    """Layer Normalization using KohakuTPU compiler's layernorm kernels."""

    def __init__(
        self,
        dim: int,
        eps: float = 1e-6,
        elementwise_affine: bool = True,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.dim = dim
        self.eps = eps
        self.elementwise_affine = elementwise_affine
        self.device = device
        self.dtype = dtype

        if elementwise_affine:
            w_np = np.ones((dim,), dtype=np.float16)
            b_np = np.zeros((dim,), dtype=np.float16)
            self.weight: Tensor | None = Tensor(w_np, device=device, dtype=dtype)
            self.bias: Tensor | None = Tensor(b_np, device=device, dtype=dtype)
        else:
            self.weight = None
            self.bias = None

    def __call__(self, x: Tensor) -> Tensor:
        orig_shape = x.shape
        x_2d = x.reshape(-1, self.dim)
        M, D = x_2d.shape
        w = (
            self.weight
            if self.weight is not None
            else Tensor(np.ones((self.dim,), dtype=np.float16), device=self.device)
        )
        b = (
            self.bias
            if self.bias is not None
            else Tensor(np.zeros((self.dim,), dtype=np.float16), device=self.device)
        )

        if D <= 128:
            out_2d = ktpu_layernorm(x_2d, w, b, eps=self.eps).realize()
        else:
            # Hierarchical wide-row LayerNorm on KohakuTPU vector core
            dev = Device[self.device].rt
            rows = split(D)
            sub_cols = D // rows
            x_np = x_2d.numpy()
            w_np = w.numpy()
            b_np = b.numpy()
            x_split = dev.tensor(x_np.reshape(M, rows, sub_cols))
            w_split = dev.tensor(
                np.broadcast_to(w_np.reshape(1, rows, sub_cols), (M, rows, sub_cols))
            )
            b_split = dev.tensor(
                np.broadcast_to(b_np.reshape(1, rows, sub_cols), (M, rows, sub_cols))
            )
            out_split = layernorm_wide(
                x_split, w_split, b_split, rows=rows, eps=self.eps
            )
            out_2d = Tensor(
                out_split.numpy().reshape(M, D), device=self.device, dtype=self.dtype
            )
            x_split.release()
            w_split.release()
            b_split.release()
            out_split.release()

        if len(orig_shape) > 2:
            return out_2d.reshape(*orig_shape)
        return out_2d


class TimestepEmbedder:
    """Sinusoidal timestep embedding followed by Linear projections on KohakuTPU."""

    def __init__(
        self,
        hidden_size: int,
        frequency_embedding_size: int = 256,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.hidden_size = hidden_size
        self.frequency_embedding_size = frequency_embedding_size
        self.device = device
        self.dtype = dtype

        self.mlp_in = Linear(
            frequency_embedding_size, hidden_size, bias=True, device=device, dtype=dtype
        )
        self.mlp_out = Linear(
            hidden_size, hidden_size, bias=True, device=device, dtype=dtype
        )

    def timestep_embedding(
        self, timesteps: Tensor, dim: int, max_period: float = 10000.0
    ) -> Tensor:
        half = dim // 2
        t_np = np.atleast_1d(timesteps.numpy()).astype(np.float32)
        freqs = np.exp(
            -math.log(max_period) * np.arange(0, half, dtype=np.float32) / half
        )
        args = t_np[:, None] * freqs[None, :]
        sin_part = np.sin(args)
        cos_part = np.cos(args)
        emb_np = np.concatenate([sin_part, cos_part], axis=-1).astype(np.float16)
        return Tensor(emb_np, device=self.device, dtype=self.dtype)

    def __call__(self, timesteps: Tensor) -> Tensor:
        emb = self.timestep_embedding(timesteps, self.frequency_embedding_size)
        h = self.mlp_in(emb).silu().realize()
        out = self.mlp_out(h).realize()
        return out


class AdaLNLoRAModulation:
    """AdaLN-LoRA bottleneck modulation module for transformer blocks on KTPU."""

    def __init__(
        self,
        hidden_size: int,
        n_chunks: int = 3,
        lora_dim: int = 256,
        use_lora: bool = True,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.hidden_size = hidden_size
        self.n_chunks = n_chunks
        self.use_lora = use_lora
        self.device = device
        self.dtype = dtype

        if use_lora:
            self.linear1 = Linear(
                hidden_size, lora_dim, bias=False, device=device, dtype=dtype
            )
            self.linear2 = Linear(
                lora_dim, n_chunks * hidden_size, bias=False, device=device, dtype=dtype
            )
        else:
            self.linear = Linear(
                hidden_size,
                n_chunks * hidden_size,
                bias=False,
                device=device,
                dtype=dtype,
            )

    def __call__(self, emb: Tensor) -> list[Tensor]:
        h = emb.silu().realize()
        if self.use_lora:
            out = self.linear2(self.linear1(h)).realize()
        else:
            out = self.linear(h).realize()

        chunk_size = self.hidden_size
        chunks = []
        out_np = out.numpy()
        for i in range(self.n_chunks):
            c_np = np.ascontiguousarray(
                out_np[..., i * chunk_size : (i + 1) * chunk_size]
            )
            chunks.append(Tensor(c_np, device=self.device, dtype=self.dtype))
        return chunks


class PatchEmbed:
    """Projects 2D image latents (B, C=16, H, W) into token embeddings on KohakuTPU."""

    def __init__(
        self,
        in_channels: int = 16,
        patch_spatial: int = 2,
        hidden_size: int = 2048,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.in_channels = in_channels
        self.patch_spatial = patch_spatial
        self.hidden_size = hidden_size
        self.patch_dim = in_channels * patch_spatial * patch_spatial
        self.device = device
        self.dtype = dtype

        self.proj = Linear(
            self.patch_dim, hidden_size, bias=False, device=device, dtype=dtype
        )

    def __call__(self, x: Tensor) -> tuple[Tensor, tuple[int, int]]:
        B, C, H, W = x.shape
        p = self.patch_spatial
        Hp, Wp = H // p, W // p

        x_np = x.numpy()
        x_patches = np.ascontiguousarray(
            x_np.reshape(B, C, Hp, p, Wp, p)
            .transpose(0, 2, 4, 1, 3, 5)
            .reshape(B, Hp * Wp, self.patch_dim)
        )
        x_flat = Tensor(x_patches, device=self.device, dtype=self.dtype)

        out = self.proj(x_flat)
        return out, (Hp, Wp)


class FinalLayer:
    """Final LayerNorm + AdaLN modulation + unpatchify to 2D latents (B, C, H, W) on KohakuTPU."""

    def __init__(
        self,
        hidden_size: int,
        patch_spatial: int = 2,
        out_channels: int = 16,
        use_adaln_lora: bool = True,
        adaln_lora_dim: int = 256,
        device: str = "KTPU",
        dtype=dtypes.float16,
    ):
        self.hidden_size = hidden_size
        self.patch_spatial = patch_spatial
        self.out_channels = out_channels
        self.patch_dim = out_channels * patch_spatial * patch_spatial
        self.device = device
        self.dtype = dtype

        self.norm = LayerNorm(
            hidden_size, elementwise_affine=False, eps=1e-6, device=device, dtype=dtype
        )
        self.linear = Linear(
            hidden_size, self.patch_dim, bias=False, device=device, dtype=dtype
        )
        self.adaln = AdaLNLoRAModulation(
            hidden_size,
            n_chunks=2,
            lora_dim=adaln_lora_dim,
            use_lora=use_adaln_lora,
            device=device,
            dtype=dtype,
        )

    def __call__(self, x: Tensor, emb: Tensor, grid_size: tuple[int, int]) -> Tensor:
        if len(emb.shape) == 2:
            emb = emb.unsqueeze(1)

        shift, scale = self.adaln(emb)
        normed = self.norm(x)
        modulated = (normed * (1.0 + scale).realize() + shift).realize()

        projected = self.linear(modulated)

        B, L, _ = projected.shape
        Hp, Wp = grid_size
        p = self.patch_spatial
        C = self.out_channels

        proj_np = projected.numpy()
        unflat = np.ascontiguousarray(
            proj_np.reshape(B, Hp, Wp, C, p, p)
            .transpose(0, 3, 1, 4, 2, 5)
            .reshape(B, C, Hp * p, Wp * p)
        )
        out = Tensor(unflat, device=self.device, dtype=self.dtype)
        return out
