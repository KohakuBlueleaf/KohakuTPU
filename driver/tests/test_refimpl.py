"""Our MX quantiser must agree with third-party production implementations.

This is the test that makes the format comparison mean anything. Without it we
would be comparing our candidate against our own yardstick, and any error in
the yardstick would read as a result about the candidate.
"""

import numpy as np
import pytest

from kohakutpu import formats as F
from kohakutpu import refimpl

KINDS = ["E4M3", "E5M2", "E2M3", "E3M2", "E2M1"]

have = refimpl.available()
need_mx = pytest.mark.skipif(not have["microxcaling"], reason="microxcaling missing")
need_ao = pytest.mark.skipif(not have["torchao"], reason="torchao missing")


def _data(seed=0, rows=16, k=128):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((rows, k)) * rng.choice([0.01, 1.0, 30.0], (rows, 1))
    x[0, :8] = [0.0, 1e-9, -1e-9, 1e4, -1e4, 0.5, -0.5, 123.4]  # awkward cases
    return x


@need_mx
@pytest.mark.parametrize("kind", KINDS)
def test_matches_microxcaling(kind):
    x = _data()
    ours = F.quantise_mx(x, kind)
    theirs = refimpl.microxcaling_quantise(x, kind)
    # exact: both emulate the same spec, so any difference is a real disagreement
    assert ours == pytest.approx(theirs, rel=0, abs=0), (
        f"{kind}: {(ours != theirs).sum()} of {ours.size} elements differ; "
        f"worst {np.abs(ours - theirs).max()}"
    )


@need_ao
@pytest.mark.parametrize("kind", ["E4M3", "E5M2"])
def test_matches_torchao(kind):
    x = _data()
    ours = F.quantise_mx(x, kind)
    theirs = refimpl.torchao_quantise(x, kind)
    assert ours == pytest.approx(theirs, rel=0, abs=0), (
        f"{kind}: {(ours != theirs).sum()} of {ours.size} elements differ; "
        f"worst {np.abs(ours - theirs).max()}"
    )


@need_mx
def test_matmul_reference_matches_microxcaling():
    """The MXFP8 matmul reference, end to end, against theirs."""
    rng = np.random.default_rng(5)
    a = rng.standard_normal((16, 64))
    bt = rng.standard_normal((16, 64))
    ours = F.matmul_mxfp8_fp32(a, bt)
    qa = refimpl.microxcaling_quantise(a, "E4M3").astype(np.float32)
    qb = refimpl.microxcaling_quantise(bt, "E4M3").astype(np.float32)
    theirs = (qa @ qb.T).astype(np.float64)
    assert ours == pytest.approx(theirs, rel=1e-12, abs=0)
