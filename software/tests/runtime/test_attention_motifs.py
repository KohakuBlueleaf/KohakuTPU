"""Eager and tinygrad share contract-based direct-ring admission."""

from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
from test_attention import reference
from test_forms import tensor as tiny_tensor

from kohakutpu.application.api.attention import attention
from kohakutpu.application.api.tensor import tensor
from kohakutpu.application.api.tinygrad_attention import attention as tiny_attention
from kohakutpu.application.kernels.attention import flash_reason


def test_direct_ring_routing_requires_its_contract_in_both_apis(dev):
    rng = np.random.default_rng(41)
    q = rng.uniform(-0.125, 0.125, (1, 128, 64)).astype(np.float16)
    k = rng.uniform(-0.125, 0.125, (1, 1024, 64)).astype(np.float16)
    v = rng.uniform(0.5, 1.0, (1, 1024, 64)).astype(np.float16)
    eager = (tensor(q), tensor(k), tensor(v.swapaxes(-1, -2)))
    tiny = tuple(tiny_tensor(x) for x in (q, k, v))
    want = reference(q, k, v)
    cases: list[tuple[Callable[..., Any], tuple[Any, ...]]] = [
        (attention, eager),
        (tiny_attention, tiny),
    ]
    for api, inputs in cases:
        start = len(dev.runs)
        with pytest.raises(
            ValueError, match="flashd requires the unit-scores contract"
        ):
            api(*inputs, motif="flashd")
        assert not any("attention" in r.info for r in dev.runs[start:])
        start = len(dev.runs)
        got = api(*inputs, contract="unit-scores").numpy().astype(np.float32)
        calls = [r for r in dev.runs[start:] if "attention" in r.info]
        assert [r.info["attention"] for r in calls] == ["attention.flashd"]
        assert "unit-scores" in calls[0].info["attention_reason"]
        assert "flash_d" in calls[0].info["compiled"].l1.source
        absolute = np.abs(got - want)
        nonzero = want != 0
        relative = absolute[nonzero] / np.abs(want[nonzero])
        for label, errors in (("abs", absolute), ("rel", relative)):
            print(
                f"{api.__module__} vs f32 {label}: max={errors.max():.6g} "
                f"mean={errors.mean():.6g} median={np.median(errors):.6g} "
                f"nonfinite={np.count_nonzero(~np.isfinite(got))}"
            )
        print(f"zero-reference absolute errors: {absolute[~nonzero]}")
    selected, reason = flash_reason(
        1,
        128,
        1024,
        64,
        precise_scores=True,
        contract="unit-scores",
        machine=dev.machine,
    )
    assert selected is None and reason == "precise scores requested"
