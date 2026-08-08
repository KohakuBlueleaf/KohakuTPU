"""The three conversion facts the compiler reasons about.

These are not arbitrary examples. Each one is a claim made in
docs/compute/vector-core.md that a pass will rely on, so if the format
definitions drift the claim fails here rather than in a datapath.
"""

import pytest

from ktpu.ir import dtype as dt


def test_fp16_into_e8m15_is_exact():
    """The reason the vector core needs no saturation logic on input."""
    assert dt.E8M15.contains_range_of(dt.FP16)
    assert dt.E8M15.is_exact_from(dt.FP16)
    assert not dt.E8M15.may_saturate_from(dt.FP16)


def test_e8m15_into_fp32_is_exact():
    """E8 IS FP32's exponent, so this direction cannot lose range either."""
    assert dt.FP32.is_exact_from(dt.E8M15)


def test_fp32_into_e8m15_is_range_lossless_but_rounds():
    """The honest half: E8M15 accepts FP32's range and drops mantissa."""
    assert dt.E8M15.contains_range_of(dt.FP32)
    assert not dt.E8M15.is_exact_from(dt.FP32)


def test_acc24_into_e8m15_is_range_lossless():
    """The split-K epilogue's whole basis: E7 is strictly inside E8."""
    assert dt.E8M15.contains_range_of(dt.ACC24)
    # ...and costs exactly one mantissa bit, not more.
    assert dt.ACC24.man - dt.E8M15.man == 1
    assert not dt.E8M15.is_exact_from(dt.ACC24)


def test_fp16_saturates_from_everything_wider():
    """Why an FP16 store is a possible range failure worth warning about."""
    for wide in (dt.FP32, dt.E8M15, dt.ACC24):
        assert dt.FP16.may_saturate_from(wide), wide.name


def test_fp16_ceiling_is_65504():
    """The number the saturation warning is keyed on. mx_fpacc.v:582."""
    assert dt.FP16.max_finite == pytest.approx(65504.0)


def test_accumulator_range_dwarfs_the_output_format():
    """The overflow is created by the FP16 store, NOT by accumulating.

    ACC24 reaches ~2^64, nine orders of magnitude above FP16's ceiling, so a
    K-sweep never loses the value -- EMIT does.
    """
    assert dt.ACC24.max_finite > 1e19
    assert dt.ACC24.max_finite / dt.FP16.max_finite > 1e14


def test_e8m15_has_no_subnormals_and_that_is_deliberate():
    assert not dt.E8M15.subnormal
    assert not dt.ACC24.subnormal
    assert dt.FP16.subnormal and dt.FP32.subnormal


def test_significand_widths():
    """16 significand bits is why the FMA fits one DSP -- vector-core.md s1.2."""
    assert dt.E8M15.significand == 16
    assert dt.FP16.significand == 11
    assert dt.FP32.significand == 24


def test_half_ulp_matches_the_measured_alu():
    """vec_alu_tb measures the FMA correctly rounded at 0.500 ulp."""
    assert dt.E8M15.eps == pytest.approx(2.0**-16)
    assert dt.FP16.eps == pytest.approx(2.0**-11)


def test_mxfp7_is_block_scaled_not_a_float():
    assert dt.MXFP7.kind is dt.Kind.BLOCK
    assert dt.MXFP7.block == 32
    assert dt.MXFP7.scale is dt.E5M3
    with pytest.raises(TypeError):
        _ = dt.MXFP7.max_finite


def test_host_sees_only_fp32_and_fp16():
    """Software never sees MXFP7 or E8M15; that is an architectural invariant."""
    assert dt.HOST_DTYPES == (dt.FP32, dt.FP16)
    assert dt.MXFP7 not in dt.HOST_DTYPES
    assert dt.E8M15 not in dt.HOST_DTYPES
