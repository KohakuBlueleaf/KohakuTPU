"""The lane format E8M15 against FP32 bit patterns rounded at bit 8."""

from types import SimpleNamespace

import numpy as np
import pytest

from kohakutpu.language.numerics import e8m15, fp16, rounding


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


#: Inputs and, per form, the bits the Verilated v9t6 card returned (zero_sign
#: probe 3, `vlt_card_v9t6s_1n`), with each form's lane rule.
PROBED = ["8000", "0000", "bc00", "3c00", "c200", "0001", "8001", "7bff", "fbff"]
PROBED += ["7c00", "fc00", "7e00"]
LANE_FORMS = {
    "vadd d, k0, x": (
        lambda x: e8m15.fma(0.0, 1.0, x),
        "8000 0000 bc00 3c00 c200 0001 8001 7bff fbff 7c00 fc00 7e00",
    ),
    "vadd d, x, k0": (
        lambda x: e8m15.fma(x, 1.0, 0.0),
        "0000 0000 bc00 3c00 c200 0001 8001 7bff fbff 7c00 fc00 7e00",
    ),
    "vmul d, k1, x": (
        lambda x: e8m15.fma(1.0, x, 0.0),
        "0000 0000 bc00 3c00 c200 0001 8001 7bff fbff 7c00 fc00 7e00",
    ),
    "vfma d, k0, x, x": (
        lambda x: e8m15.fma(0.0, x, x),
        "8000 0000 bc00 3c00 c200 0001 8001 7bff fbff 7e00 7e00 7e00",
    ),
    "vsub d, k0, x": (
        lambda x: e8m15.fma(0.0, 1.0, -x),
        "0000 8000 3c00 bc00 4200 8001 0001 fbff 7bff fc00 7c00 7e00",
    ),
    "vadd d, x, -0": (
        lambda x: e8m15.fma(x, 1.0, -0.0),
        "8000 8000 bc00 3c00 c200 0001 8001 7bff fbff 7c00 fc00 7e00",
    ),
    "vneg d, x": (
        lambda x: e8m15.moved(-x),
        "0000 0000 3c00 bc00 4200 8001 0001 fbff 7bff fc00 7c00 7e00",
    ),
    "vmov d, x": (
        lambda x: e8m15.moved(x),
        "0000 0000 bc00 3c00 c200 0001 8001 7bff fbff 7c00 fc00 7e00",
    ),
    "vmax d, x, k0": (
        lambda x: e8m15.moved(np.where(x < 0, 0.0, x)),
        "0000 0000 0000 3c00 0000 0001 0000 7bff 0000 7c00 0000 7e00",
    ),
    "vmin d, x, k0": (
        lambda x: e8m15.moved(np.where(x > 0, 0.0, x)),
        "0000 0000 bc00 0000 c200 0000 8001 0000 fbff 0000 fc00 7e00",
    ),
}


def _f16_bits(x):
    """f16 bits of `x`, a NaN as 7e00."""
    x = np.atleast_1d(np.asarray(x, np.float64)).astype(np.float16)
    bits = x.view(np.uint16).copy()
    bits[np.isnan(x)] = 0x7E00
    return " ".join(f"{int(b):04x}" for b in bits)


@pytest.mark.parametrize("form", list(LANE_FORMS))
def test_lane_rules_return_the_probed_bits(form):
    x = np.array([int(b, 16) for b in PROBED], np.uint16).view(np.float16)
    rule, want = LANE_FORMS[form]
    assert _f16_bits(rule(x.astype(np.float64))) == want


def test_the_lane_zero_rules_are_not_ieee():
    with np.errstate(all="ignore"):
        assert _f16_bits(np.float64(0.0) + np.float64(-0.0)) == "0000"
        assert _f16_bits(e8m15.fma(0.0, 1.0, -0.0)) == "8000"
        assert _f16_bits(-np.float64(0.0)) == "8000"
        assert _f16_bits(e8m15.moved(-np.float64(0.0))) == "0000"


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


@pytest.mark.parametrize(
    "x,want",
    [
        (1 + 3 * 2.0**-17, 0x3F8001),
        (1 + 2.0**-16, 0x3F8000),
        (1e39, 0x7F8000),
        (-1e39, 0xFF8000),
        (-0.0, 0x800000),
        (np.nextafter(2.0**-126, 0), 0),
        (-np.nextafter(2.0**-126, 0), 0x800000),
        (2.0**-126, 0x008000),
        (float("nan"), 0x7FC000),
    ],
)
def test_constant_words(x, want):
    assert e8m15.encode(x) == want


def test_fp16_write_saturation_and_ties():
    x = [70000, -1e6, 65520, 65504, np.inf, -np.inf, 1 + 2.0**-11, 2.0**-24]
    assert fp16.write(x).view(np.uint16).tolist() == [
        0x7BFF,
        0xFBFF,
        0x7BFF,
        0x7BFF,
        0x7C00,
        0xFC00,
        0x3C00,
        1,
    ]
    assert np.isposinf(fp16.write(np.inf, preserve_infinity=True))
    assert fp16.write(2.0**-24, flush_subnormals=True) == 0
    assert np.isnan(fp16.write(np.nan))
    assert np.signbit(fp16.write(-0.0))


def test_fp16_read_consumers_are_explicit():
    bits = [0x7C00, 1, 0x7BFF, 0x8000]
    assert np.array_equal(
        fp16.values(bits, consumer="vector"), [np.inf, 2.0**-24, 65504, -0.0]
    )
    for consumer in ("cluster", "sru"):
        assert np.array_equal(
            fp16.values(bits, consumer=consumer), [65536, 0, 65504, -0.0]
        )
    with pytest.raises(ValueError):
        fp16.values(bits, consumer="unknown")
    with pytest.raises(TypeError):
        fp16.values(bits)


class Lane:
    def __init__(self, x):
        self.x = e8m15.value(x)

    @staticmethod
    def of(x):
        return x.x if isinstance(x, Lane) else e8m15.value(x)

    def __add__(self, x):
        return Lane(self.x + self.of(x))

    def __sub__(self, x):
        return Lane(self.x - self.of(x))


ARITH = SimpleNamespace(
    math=lambda op, a, b: Lane(
        {"cmplt": np.less, "cmpgt": np.greater}[op](Lane.of(a), Lane.of(b))
    ),
    where=lambda c, a, b: Lane(np.where(Lane.of(c) != 0, Lane.of(a), Lane.of(b))),
    absolute=lambda a: Lane(np.abs(Lane.of(a))),
)


@pytest.mark.parametrize("sig_bits", [12, 16, 18])
def test_trunc_covers_both_signs_and_the_whole_fractional_range(sig_bits, monkeypatch):
    monkeypatch.setattr(e8m15, "SIGNIFICAND", sig_bits)
    limit = rounding.whole(sig_bits)
    xs = np.arange(-limit - 2, limit + 2, 0.5)
    xs = np.concatenate([xs, [-0.25, -0.75, 0.25, 0.75]])
    x = Lane(xs)
    assert np.array_equal(rounding.trunc(x, ARITH, sig_bits).x, np.trunc(x.x))
