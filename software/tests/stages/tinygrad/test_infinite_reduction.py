"""Inactive row lanes do not defeat infinite live extrema."""

import numpy as np
import pytest
from tinygrad.tensor import Tensor


@pytest.mark.parametrize("minimum", [False, True])
def test_partial_row_extrema_preserve_infinity(dev, minimum):
    data = np.full((4, 17), np.inf if minimum else -np.inf, dtype=np.float16)
    x = Tensor(data, device="KTPU")
    result = x.min(1) if minimum else x.max(1)
    np.testing.assert_array_equal(result.numpy(), data[:, 0])
