"""The runtime device the card tests share: the Verilated card model the
runtime opens (``KOHAKUTPU_SIM_BUILD``, ``KOHAKUTPU_SIM_NODES``); every test
using it is skipped when that model is not built."""

import numpy as np
import pytest

from kohakutpu.application.runtime import device as D


@pytest.fixture(scope="session")
def dev():
    try:
        d = D.device()
    except FileNotFoundError as e:
        pytest.skip(str(e))
    yield d


@pytest.fixture(scope="session")
def rel():
    """max |got - want| / max |want|, in float64."""

    def measure(got, want) -> float:
        got, want = np.asarray(got, np.float64), np.asarray(want, np.float64)
        return float(np.abs(got - want).max() / max(np.abs(want).max(), 1e-9))

    return measure
