"""Unit tests for Anima DiT backbone on KohakuTPU."""

import ktpugrad
import numpy as np
import pytest
from tinygrad import Tensor

from demos.kohakutpu.anima.config import AnimaConfig
from demos.kohakutpu.anima.models.dit import AnimaDiT


@pytest.fixture(scope="module", autouse=True)
def setup_ktpu():
    ktpugrad.install()
    yield
    ktpugrad.uninstall()


def test_anima_dit_forward_pass():
    cfg = AnimaConfig.tiny(device="KTPU")
    dit = AnimaDiT(cfg)

    # Input latents: (B=1, C=16, H=16, W=16) -> (16/2)*(16/2) = 64 tokens (matches flash_attention block=64)
    x = Tensor(np.random.randn(1, 16, 16, 16).astype(np.float16), device="KTPU")
    t = Tensor(np.array([250.0], dtype=np.float16), device="KTPU")
    context = Tensor(
        np.random.randn(1, 64, cfg.crossattn_emb_channels).astype(np.float16),
        device="KTPU",
    )

    out = dit(x, t, context).realize()
    assert out.shape == (1, 16, 16, 16)
    assert not np.isnan(out.numpy()).any()
