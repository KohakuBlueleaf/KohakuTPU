"""Tinygrad entry for bounded, masked device-side DSL attention."""

import numpy as np
from tinygrad import Tensor, dtypes

from kohakutpu.application.api.attention import attention as device_attention
from kohakutpu.application.api.tensor import Buf
from kohakutpu.application.api.tensor import Tensor as DeviceTensor
from kohakutpu.application.ktpugrad.bridge import _realized, _storage
from kohakutpu.application.runtime import forms as F
from kohakutpu.application.runtime.storage import FLAT, Storage


def attention(q: Tensor, k: Tensor, v: Tensor) -> Tensor:
    """Three-axis scaled attention with unquantized QK and split probabilities.

    Realized KTPU buffers are borrowed, never downloaded. The DSL adapter owns
    padded side forms and masks; the returned tinygrad tensor owns its output.
    """
    if any(t.dtype not in (dtypes.half, dtypes.float) for t in (q, k, v)):
        raise ValueError("attention accepts f16 or f32 tensors")
    tensors = _realized((q, k, v.transpose(-1, -2)))
    borrowed = []
    for t in tensors:
        original = _storage(t)
        addr = F.f16(
            original.dev,
            original,
            np.float16 if t.dtype == dtypes.half else np.float32,
            t.numel(),
        )
        st = Storage(original.dev, 2 * t.numel(), addr=addr, owned=False)
        borrowed.append(DeviceTensor(t.shape, buf=Buf(st, np.float16)))
    result = device_attention(*borrowed, precise_scores=True)
    out = Tensor.empty(*result.shape, dtype=dtypes.half, device="KTPU").realize()
    target = _storage(out)
    F.relayout(
        target.dev,
        result.st.addr,
        result.size,
        ((result.size, 1),),
        0,
        ((result.size, 1),),
        result.size,
        target.addr,
    )
    target.wrote(FLAT)
    target.dtype = np.dtype(np.float16)
    return out
