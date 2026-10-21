"""Plans without a card: which casts cost lane ops, and a reduction the
expression reads twice."""

import numpy as np
import pytest
from tinygrad.tensor import Tensor
from tinygrad.uop import Ops

from kohakutpu.application import ktpugrad  # noqa: F401
from kohakutpu.application.kernels import expr as L
from kohakutpu.application.ktpugrad import lower as LW

RNG = np.random.default_rng(11)


def _exprs(t: Tensor) -> str:
    linear, _ = Tensor.linear_with_vars(t)
    asts = [c.src[0] for c in linear.toposort() if c.op is Ops.CALL]
    return repr([LW.plan(a).expr for a in asts if a.op is Ops.SINK])


@pytest.mark.parametrize(
    "make, cmpne",
    [
        (lambda x: (x > 0).half() * x, False),  # a bool is 0 / 1 already
        (lambda x: x.bool().half(), True),  # a value compares with 0
    ],
)
def test_a_cast_costs_ops_only_where_the_value_changes(make, cmpne):
    x = Tensor(RNG.standard_normal((4, 16)).astype(np.float16), device="KTPU")
    got = _exprs(make(x))
    assert ("'cmpne'" in got) == cmpne and "'trunc'" not in got


def test_a_reduction_read_twice_is_one_reduction():
    """``x - mean`` and ``(x - mean) * mean``: the tree holds one reduction."""
    m = L.Reduce("sum", L.Load(0))
    e = L.op("mul", L.op("sub", L.Load(1), m), m)
    assert L.reduces(e) == [m]
    assert len(L.reduces(L.op("add", m, L.Reduce("sum", L.Load(0))))) == 2
