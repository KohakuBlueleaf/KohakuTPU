"""Distribution/margin diagnostics and non-invasive ViT block observation."""

import numpy as np
import pytest
from examples.tinygrad.vit_tiny import seeded_model
from tinygrad import Tensor

from kohakutpu.application.checks.accuracy import logit_report, state_error


def test_margin_weighting_discount_boundary_swap_but_not_large_margin_loss():
    want = np.array([[10, 8, 6, 4, 1.01, 1, 0]], dtype=np.float32)
    boundary = want.copy()
    boundary[0, 4:6] = [1, 1.01]
    severe = want.copy()
    severe[0, [0, 5]] = [1, 10]
    a, b = logit_report(boundary, want), logit_report(severe, want)
    assert a["top5_mean_overlap"] == b["top5_mean_overlap"] == 0.8
    assert a["margin_weighted_topk_agreement"] > 0.999
    assert b["margin_weighted_topk_agreement"] < 0.7
    assert a["diagnostic_budget"]["all_passed"]
    assert not b["diagnostic_budget"]["all_passed"]
    assert (
        logit_report(np.ones((1, 8)), np.ones((1, 8)))["margin_weighted_topk_agreement"]
        is None
    )


def test_distributions_match_per_input_measurements_and_reject_nonfinite():
    want = np.array([[1, 0, -1], [2, 0, -2]], dtype=np.float32)
    got = want + np.array([[0.01, 0, 0], [0.2, 0, 0]])
    result = logit_report(got, want)
    assert result["error_distributions"]["max_absolute_error"]["p50"] == pytest.approx(
        0.105
    )
    assert result["diagnostic_budget"]["inputs_within_error_budget"] == 1
    assert state_error(want, want)["cosine_similarity"] == pytest.approx(1)
    with pytest.raises(ValueError):
        state_error([np.nan], [0])


def test_cpu_vit_observer_records_every_block_without_changing_output():
    model = seeded_model(2, 17, width=64, image_size=16)
    image = np.random.default_rng(18).standard_normal((1, 3, 16, 16)).astype(np.float32)
    plain = model(Tensor(image, device="CPU")).numpy()
    states = {}

    def observe(stage, value):
        states[stage] = value.numpy().copy()

    traced = model(Tensor(image, device="CPU"), observe).numpy()
    np.testing.assert_array_equal(plain, traced)
    assert list(states) == ["embedding", "block_1", "block_2", "final_norm"]
    assert all(s.shape == (1, 2, 64) for s in states.values())
