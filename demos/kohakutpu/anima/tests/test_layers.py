"""Unit tests for Anima neural network layers on KohakuTPU."""

import ktpugrad
import numpy as np
import pytest
from tinygrad import Tensor

from demos.kohakutpu.anima.models.layers import (
    AdaLNLoRAModulation,
    FinalLayer,
    LayerNorm,
    Linear,
    PatchEmbed,
    RMSNorm,
    TimestepEmbedder,
)


@pytest.fixture(scope="module", autouse=True)
def setup_ktpu():
    ktpugrad.install()
    yield
    ktpugrad.uninstall()


def test_linear_on_ktpu():
    layer = Linear(64, 128, bias=True, device="KTPU")
    x = Tensor(np.random.randn(8, 64).astype(np.float16), device="KTPU")
    out = layer(x).realize()
    assert out.shape == (8, 128)
    assert not np.isnan(out.numpy()).any()


def test_rmsnorm_on_ktpu():
    norm = RMSNorm(64, eps=1e-6, device="KTPU")
    x = Tensor(np.random.randn(8, 64).astype(np.float16), device="KTPU")
    out = norm(x).realize()
    assert out.shape == (8, 64)
    rms = np.sqrt(np.mean(out.numpy() ** 2, axis=-1))
    np.testing.assert_allclose(rms, 1.0, atol=0.1)


def test_layernorm_on_ktpu():
    norm = LayerNorm(64, eps=1e-6, device="KTPU")
    x = Tensor(np.random.randn(8, 64).astype(np.float16), device="KTPU")
    out = norm(x).realize()
    assert out.shape == (8, 64)
    mean = np.mean(out.numpy(), axis=-1)
    np.testing.assert_allclose(mean, 0.0, atol=0.1)


def test_timestep_embedder_on_ktpu():
    t_emb = TimestepEmbedder(
        hidden_size=256, frequency_embedding_size=256, device="KTPU"
    )
    t = Tensor(np.array([100.0, 500.0], dtype=np.float16), device="KTPU")
    out = t_emb(t).realize()
    assert out.shape == (2, 256)
    assert not np.isnan(out.numpy()).any()


def test_adaln_lora_on_ktpu():
    adaln = AdaLNLoRAModulation(
        hidden_size=128, n_chunks=3, lora_dim=64, use_lora=True, device="KTPU"
    )
    emb = Tensor(np.random.randn(2, 128).astype(np.float16), device="KTPU")
    chunks = adaln(emb)
    assert len(chunks) == 3
    for c in chunks:
        c_real = c.realize()
        assert c_real.shape == (2, 128)


def test_patch_embed_and_final_layer_roundtrip():
    patch_embed = PatchEmbed(
        in_channels=16, patch_spatial=2, hidden_size=128, device="KTPU"
    )
    final_layer = FinalLayer(
        hidden_size=128, patch_spatial=2, out_channels=16, device="KTPU"
    )

    # 2D latent image: (B=1, C=16, H=16, W=16)
    x = Tensor(np.random.randn(1, 16, 16, 16).astype(np.float16), device="KTPU")
    tokens, grid_size = patch_embed(x)
    tokens = tokens.realize()
    assert tokens.shape == (1, 8 * 8, 128)
    assert grid_size == (8, 8)

    emb = Tensor(np.random.randn(1, 128).astype(np.float16), device="KTPU")
    reconstructed = final_layer(tokens, emb, grid_size).realize()
    assert reconstructed.shape == (1, 16, 16, 16)
