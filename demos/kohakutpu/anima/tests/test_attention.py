"""Unit tests for Multi-Head Self-Attention and Cross-Attention on KohakuTPU."""

import ktpugrad
import numpy as np
import pytest
from tinygrad import Tensor

from demos.kohakutpu.anima.models.attention import CrossAttention, SelfAttention
from demos.kohakutpu.anima.models.rope import SpatialRopeEmbedding2D


@pytest.fixture(scope="module", autouse=True)
def setup_ktpu():
    ktpugrad.install()
    yield
    ktpugrad.uninstall()


def test_self_attention_with_rope():
    num_heads = 4
    head_dim = 64
    query_dim = num_heads * head_dim  # 256
    L = 64  # aligned to block=64 for KTPU flash_attention

    self_attn = SelfAttention(
        query_dim=query_dim, num_heads=num_heads, head_dim=head_dim, device="KTPU"
    )
    rope = SpatialRopeEmbedding2D(head_dim=head_dim)

    grid_size = (8, 8)
    cos, sin, P = rope.get_rotary_cos_sin(grid_size, device="KTPU")

    x = Tensor(np.random.randn(1, L, query_dim).astype(np.float16), device="KTPU")
    out = self_attn(x, rope_cos=cos, rope_sin=sin, rope_P=P).realize()
    assert out.shape == (1, L, query_dim)
    assert not np.isnan(out.numpy()).any()


def test_cross_attention():
    num_heads = 4
    head_dim = 64
    query_dim = num_heads * head_dim  # 256
    context_dim = 128
    L = 64

    cross_attn = CrossAttention(
        query_dim=query_dim,
        context_dim=context_dim,
        num_heads=num_heads,
        head_dim=head_dim,
        device="KTPU",
    )

    x = Tensor(np.random.randn(1, L, query_dim).astype(np.float16), device="KTPU")
    context = Tensor(
        np.random.randn(1, L, context_dim).astype(np.float16), device="KTPU"
    )
    out = cross_attn(x, context=context).realize()
    assert out.shape == (1, L, query_dim)
    assert not np.isnan(out.numpy()).any()
