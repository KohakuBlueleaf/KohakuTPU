"""End-to-end tests for Anima text-to-image pipeline on KohakuTPU."""

import ktpugrad
import pytest

from demos.kohakutpu.anima.config import AnimaConfig
from demos.kohakutpu.anima.pipeline import AnimaPipeline, RGBImage


@pytest.fixture(scope="module", autouse=True)
def setup_ktpu():
    ktpugrad.install()
    yield
    ktpugrad.uninstall()


def test_anima_pipeline_end_to_end_sampling():
    cfg = AnimaConfig.tiny(device="KTPU")
    pipe = AnimaPipeline.from_config(cfg)

    # Resolution 128x128 -> Latents 16x16 -> 64 tokens (aligned with block=64)
    img = pipe(
        prompt="1girl, anime style, colorful hair",
        negative_prompt="blurry, low quality",
        height=128,
        width=128,
        num_inference_steps=1,
        cfg_scale=1.0,
        seed=123,
        verbose=False,
    )

    assert isinstance(img, RGBImage)
    assert img.size == (128, 128)
    assert img.data.shape == (128, 128, 3)
