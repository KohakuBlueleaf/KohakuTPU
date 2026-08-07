"""Verify the MX element formats against their published value grids.

These are the numbers the OCP MX v1.0 spec fixes, and they are the only way to
catch an emax that is one binade off -- a wrong emax produces no error, no NaN,
and no overflow. It just makes the format measure worse than it is, which is
indistinguishable from the format being bad.
"""

import numpy as np
import pytest

from kohakutpu import formats as F

# Published maxima. E4M3 is 448 because OCP reserves only S.1111.111 as NaN;
# deriving it as "all-ones exponent reserved" gives 240 and is wrong.
MAXIMA = {"E4M3": 448.0, "E5M2": 57344.0, "E2M3": 7.5, "E3M2": 28.0, "E2M1": 6.0}

# MXFP4's entire positive grid, which is small enough to write down.
E2M1_GRID = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]


@pytest.mark.parametrize("kind,want", MAXIMA.items())
def test_max_representable(kind, want):
    assert F.grid(kind)[-1] == want
    assert F.ELEM[kind]["max"] == want


def test_e2m1_grid_is_exactly_the_published_set():
    assert F.grid("E2M1") == E2M1_GRID


def test_e4m3_smallest_subnormal():
    # 2^-9: exponent 0, mantissa 1 -> (1/8) * 2^-6
    assert F.grid("E4M3")[1] == pytest.approx(2.0**-9)


@pytest.mark.parametrize("kind", list(MAXIMA))
def test_rounding_lands_on_the_grid(kind):
    g = np.array(F.grid(kind))
    rng = np.random.default_rng(0)
    x = rng.uniform(-F.ELEM[kind]["max"], F.ELEM[kind]["max"], 4000)
    q = F._fp_round(x, kind)
    # every rounded value must BE a representable magnitude
    assert np.isin(np.abs(q), g).all()


@pytest.mark.parametrize("kind", list(MAXIMA))
def test_rounding_picks_the_nearest_grid_point(kind):
    g = np.array(F.grid(kind))
    rng = np.random.default_rng(1)
    x = rng.uniform(0, F.ELEM[kind]["max"], 2000)
    q = F._fp_round(x, kind)
    nearest = g[np.abs(g[None, :] - x[:, None]).argmin(axis=1)]
    assert q == pytest.approx(nearest)


def test_saturates_rather_than_wrapping():
    for kind, mx in MAXIMA.items():
        q = F._fp_round(np.array([mx * 10, -mx * 10]), kind)
        assert q[0] == mx and q[1] == -mx


def test_block_scale_puts_the_peak_in_the_top_binade():
    """The MX scale is 2^(floor(log2 peak) - emax_elem), so peak/scale lands in
    [2^emax, 2^(emax+1)) -- that is what makes the element range usable."""
    rng = np.random.default_rng(2)
    for kind in MAXIMA:
        emax = F.ELEM[kind]["emax"]
        for mult in (1e-3, 1.0, 7.3, 250.0):
            x = rng.standard_normal((4, F.KBLOCK)) * mult
            q = F.quantise_mx(x, kind)
            peak = np.abs(x).max(axis=-1)
            scale = 2.0 ** (np.floor(np.log2(peak)) - emax)
            assert np.all(np.abs(x).max(axis=-1) / scale >= 2.0**emax - 1e-9)
            assert np.all(np.abs(x).max(axis=-1) / scale < 2.0 ** (emax + 1))
            # and nothing came back larger than the block can express
            assert np.all(np.abs(q) <= F.ELEM[kind]["max"] * scale[:, None] + 1e-12)


def test_quantise_is_idempotent():
    """Re-quantising an already-quantised block must change nothing."""
    rng = np.random.default_rng(3)
    for kind in MAXIMA:
        x = rng.standard_normal((8, F.KBLOCK))
        q1 = F.quantise_mx(x, kind)
        assert F.quantise_mx(q1, kind) == pytest.approx(q1)


def test_bf16_matches_torch():
    """Our bit-twiddled bfloat16 against torch's, which is production code."""
    torch = pytest.importorskip("torch")
    rng = np.random.default_rng(7)
    x = np.concatenate(
        [
            rng.standard_normal(4000) * 10.0 ** rng.integers(-8, 8, 4000),
            np.array([0.0, -0.0, 1.0, -1.0, 2.0**-126, 3.4e38]),
        ]
    ).astype(np.float32)
    ours = F.to_bf16(x)
    theirs = torch.from_numpy(x).to(torch.bfloat16).to(torch.float32).numpy()
    assert ours == pytest.approx(theirs, rel=0, abs=0)


def test_bf16_keeps_seven_mantissa_bits():
    # everything below bit 16 of the FP32 significand must be gone
    rng = np.random.default_rng(8)
    x = rng.standard_normal(1000).astype(np.float32)
    assert np.all((F.to_bf16(x).view(np.uint32) & 0xFFFF) == 0)


def test_fp16_reference_is_actually_fp16():
    rng = np.random.default_rng(4)
    a = rng.standard_normal((8, 32))
    bt = rng.standard_normal((8, 32))
    got = F.matmul_fp16_fp32(a, bt)
    want = (
        a.astype(np.float16).astype(np.float32)
        @ bt.astype(np.float16).astype(np.float32).T
    )
    assert got == pytest.approx(want.astype(np.float64))
