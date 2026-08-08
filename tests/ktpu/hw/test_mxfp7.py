"""The E5M3 quantiser model, which is the spec mx_quant.v has to match."""

import numpy as np

from ktpu.hw import formats as F
from ktpu.hw import mxfp7


def _blocks(seed=0, rows=32, k=256):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((rows, k)) * (10.0 ** rng.integers(-3, 3, (rows, 1)))
    x[0, :6] = [0.0, -0.0, 1.0, -1.0, 65504.0, 6.1e-5]
    return x.astype(np.float32)


def test_peak_lands_near_the_top_of_int7():
    """The whole point of a scale mantissa: the peak stops falling short.

    E8M0 could only place the peak in [32,64). E5M3 gets it into [56,63], so
    the significand is always nearly fully used instead of sometimes half used.
    """
    q, _es, _m8 = mxfp7.quantise_fp16(_blocks())
    peak = np.abs(q.reshape(*q.shape[:-1], -1, mxfp7.KBLOCK)).max(axis=-1)
    nz = peak > 0
    assert peak[nz].max() <= mxfp7.QMAX
    assert peak[nz].min() >= 56


def test_never_clips_the_block_peak():
    """Rounding the scale UP is what guarantees this; rounding to nearest would
    put the largest element of some blocks above 63 and saturate it."""
    x = _blocks(1)
    q, _es, _m8 = mxfp7.quantise_fp16(x)
    assert np.abs(q).max() <= mxfp7.QMAX


def test_scale_field_fits_e5m3():
    _q, es, m8 = mxfp7.quantise_fp16(_blocks(2))
    field_e = es + mxfp7.SBIAS
    assert field_e.min() >= 0 and field_e.max() <= 31, "exponent escapes E5"
    assert m8.min() >= 8 and m8.max() <= 15, "mantissa escapes M3"


def test_matches_the_reference_int7_e5m3_within_reciprocal_rounding():
    """Against formats.quantise_mx_int, which divides exactly rather than by a
    reciprocal table. They may differ by one step where the reciprocal rounds
    the other way; more than that means the integer path is wrong."""
    # BOTH sides must see the same input. The hardware quantiser reads FP16
    # out of memory, so feeding the reference FP32 gives it a different block
    # peak, a different scale, and a disagreement that is the test's fault.
    x = _blocks(3).astype(np.float16).astype(np.float64)
    ours = mxfp7.value_fp16(x)
    ref = F.quantise_mx_int(x, 63, "E5M3")
    q, es, m8 = mxfp7.quantise_fp16(x)
    step = mxfp7.scale_value(es, m8)[..., None]
    step = np.broadcast_to(
        step, (*q.shape[:-1], q.shape[-1] // mxfp7.KBLOCK, mxfp7.KBLOCK)
    ).reshape(q.shape)
    off = np.abs(ours - ref) / step
    assert off.max() <= 1.001, f"differs by {off.max():.2f} steps"
    assert (off > 0.5).mean() < 0.02, "more than 2% of elements differ by a step"


def test_beats_the_old_e8m0_scale_on_a_real_matmul():
    rng = np.random.default_rng(4)
    w = rng.standard_normal((16, 256))
    a = (rng.standard_normal((64, 16)) @ w).astype(np.float32)
    bt = (rng.standard_normal((64, 16)) @ w).astype(np.float32)
    want = a.astype(np.float64) @ bt.astype(np.float64).T

    def p50(got):
        r = np.abs(got - want) / np.abs(want)
        return np.median(r[np.isfinite(r)])

    new = p50(mxfp7.model_matmul(a, bt))
    old = p50(F.quantise_mx_int(a, 63, "E8M0") @ F.quantise_mx_int(bt, 63, "E8M0").T)
    assert new < old, f"E5M3 {new:.3%} should beat E8M0 {old:.3%}"


def test_zero_block_is_zero():
    x = np.zeros((2, mxfp7.KBLOCK), dtype=np.float32)
    q, _es, _m8 = mxfp7.quantise_fp16(x)
    assert not q.any()
    assert not np.isnan(mxfp7.value_fp16(x)).any()


def test_anchor_cancels_both_scale_biases():
    assert mxfp7.ANCHOR == 2 * mxfp7.SBIAS
