"""Masked DSL attention compiles from exact logical allocations."""

import numpy as np
import pytest
from tinygrad import Tensor, dtypes

from kohakutpu.application import api as kp
from kohakutpu.application.api.attention import attention
from kohakutpu.application.ktpugrad.attention import attention as tg_attention
from kohakutpu.application.runtime import device as D


@pytest.mark.parametrize("precise", (False, True))
@pytest.mark.parametrize("tokens", (1, 63, 64, 65, 197, 256))
@pytest.mark.parametrize("queries", (1, 65))
def test_partial_attention(tokens, queries, precise):
    rng = np.random.default_rng(19)
    with D.attached(D.Device("offline", board="multimesh_v9t6")):
        q, k, vt = [
            kp.tensor(rng.standard_normal(shape).astype(np.float16))
            for shape in ((1, queries, 64), (1, tokens, 64), (1, 64, tokens))
        ]
        y = attention(q, k, vt, precise_scores=precise)
        assert y.shape == (1, queries, 64)
        assert y.st.nbytes == 2 * queries * 64


def test_tinygrad_attention_full_shape():
    with D.attached(D.Device("offline", board="multimesh_v9t6")):
        q, k, v = [
            Tensor.zeros(3, 197, 64, device="KTPU", dtype=dtypes.half)
            .contiguous()
            .realize()
            for _ in range(3)
        ]
        y = tg_attention(q, k, v)
        assert y.shape == (3, 197, 64)
        assert y.device == "KTPU"


def test_tinygrad_attention_rejects_integer():
    x = Tensor.zeros(1, 1, 64, dtype=dtypes.int32, device="CPU")
    with pytest.raises(ValueError, match="f16 or f32"):
        tg_attention(x, x, x)
