"""Unit tests for 2D Spatial Rotary Positional Embedding (RoPE) on KohakuTPU."""

import ktpugrad
import numpy as np
import pytest
from tinygrad import Tensor

from demos.kohakutpu.anima.models.rope import SpatialRopeEmbedding2D, apply_rotary_emb


@pytest.fixture(scope="module", autouse=True)
def setup_ktpu():
    ktpugrad.install()
    yield
    ktpugrad.uninstall()


def test_rope_table_generation():
    rope = SpatialRopeEmbedding2D(head_dim=64)
    grid_size = (8, 8)
    cos, sin, P = rope.get_rotary_cos_sin(grid_size, device="KTPU")
    cos_np = cos.numpy()
    sin_np = sin.numpy()

    L = 8 * 8
    assert cos_np.shape == (1, L, 1, 64)
    assert sin_np.shape == (1, L, 1, 64)
    np.testing.assert_allclose(cos_np**2 + sin_np**2, 1.0, atol=1e-2)


def test_apply_rotary_emb():
    rope = SpatialRopeEmbedding2D(head_dim=64)
    grid_size = (4, 4)
    cos, sin, P = rope.get_rotary_cos_sin(grid_size, device="KTPU")

    L = 4 * 4
    x = Tensor(np.random.randn(2, L, 4, 64).astype(np.float16), device="KTPU")
    rotated = apply_rotary_emb(x, cos, sin, P).realize()
    assert rotated.shape == (2, L, 4, 64)
    assert not np.isnan(rotated.numpy()).any()
