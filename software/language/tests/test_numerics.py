"""The lane format E8M15 against FP32 bit patterns rounded at bit 8."""

import numpy as np
from kohakutpu.language.numerics import e8m15


def _by_bits(x32):
    """FP32 values rounded to 15 mantissa bits, nearest even, on the bits."""
    b = x32.view(np.uint32).astype(np.uint64)
    b = (b + 0x7F + ((b >> 8) & 1)) & ~np.uint64(0xFF)
    return b.astype(np.uint32).view(np.float32).astype(np.float64)


def test_rounds_like_the_fp32_top_24_bits():
    rng = np.random.default_rng(0)
    bits = rng.integers(0x00800000, 0x7F000000, 200_000, dtype=np.uint32)
    x = bits.view(np.float32) * rng.choice([-1, 1], bits.size).astype(np.float32)
    # Ties and their neighbours: the low byte at 0x7F, 0x80, 0x81.
    ties = (bits & ~np.uint32(0xFF)) | rng.choice([0x7F, 0x80, 0x81], bits.size)
    for v in (x, ties.astype(np.uint32).view(np.float32)):
        assert np.array_equal(e8m15.value(v.astype(np.float64)), _by_bits(v))


def test_has_no_subnormals_and_keeps_the_fp32_range():
    assert e8m15.value(2.0**-127) == 0.0
    assert e8m15.value(-(2.0**-126)) == -(2.0**-126)
    assert e8m15.value(2.0**127 * 1.5) == 2.0**127 * 1.5
    assert np.isinf(e8m15.value(2.0**128))


def test_a_large_offset_rounds_to_whole_numbers():
    # flash's offset: m + 49156 - 49152 is round(m) + 4 in the lane format.
    m = np.array([0.3, 2.5, 3.5, -1.7, 7.49])
    got = e8m15.value(e8m15.value(m + 49156.0) - 49152.0)
    assert np.array_equal(got, np.round(m) + 4)
