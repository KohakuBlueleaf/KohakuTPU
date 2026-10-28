"""FP16 host constants follow the same saturation as a card write."""

import numpy as np
from tinygrad.dtype import dtypes
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp

from kohakutpu.application.ktpugrad import fold
from kohakutpu.language.numerics import fp16


def test_folded_fp16_constants_and_intermediates_saturate(monkeypatch):
    large = UOp(Ops.CONST, dtypes.half, arg=70000.0)
    assert fold.evaluate(large, ()) == 65504.0
    a = UOp(Ops.CONST, dtypes.half, arg=60000.0)
    b = UOp(Ops.ADD, dtypes.half, src=(a, a))
    assert fold.evaluate(b, ()) == 65504.0
    with monkeypatch.context() as m, np.errstate(over="ignore"):
        m.setattr(fp16, "write", lambda x, **kw: np.asarray(x, np.float16))
        assert np.isinf(fold.evaluate(b, ()))
