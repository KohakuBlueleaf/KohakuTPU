"""The norms at SDXL's REAL widths, which every one of them was refused at.

210 LayerNorms at D = 640 and 1280, 46 GroupNorms at groups of 40,960 / 81,920
/ 163,840, and 140 softmaxes at 1280 and at a 77-token context. None of those
counts is a power of two sub-rows, which is the whole reason they were refused.

Graded against float64 relative to the reference's own peak, and reported as
percentiles rather than a max: ~0.3% of elements are reproducibly wrong on this
machine and a max alone reports that defect rather than the change under test.
"""

import numpy as np
import pytest
from kohakutpu.kernels import (
    group_norm_silu_wide,
    group_norm_wide,
    layernorm_wide,
    part_for,
    softmax_wide,
    split,
)
from kohakutpu.kernels.wide import fold_flat
from kohakutpu.model import SimDevice

from kohakutpu import api
from kohakutpu import ops as _o

FP16 = np.float16

#: The two LayerNorm widths SDXL has, and the one that already ran.
WIDTHS = [128, 640, 1280]

#: The three GroupNorm group sizes, as `C/32 * H * W` at each UNet level.
GROUPS = [(1280 // 32) * 32 * 32, (640 // 32) * 64 * 64, (320 // 32) * 128 * 128]


def rel(got, want) -> np.ndarray:
    want = np.asarray(want, np.float64)
    return np.abs(np.asarray(got, np.float64) - want) / np.abs(want).max()


def grade(got, want, p99: float, over: float = 0.0):
    """Assert the p99 and the share of elements past 1%, not the max."""
    err = rel(got, want)
    assert float(np.percentile(err, 99)) < p99
    assert float((err > 0.01).mean()) <= over
    return err


def operands(shape, shift=0.0, seed=4):
    rng = np.random.default_rng(seed)
    x = np.asarray(rng.standard_normal(shape) + shift, FP16)
    w = np.asarray(rng.standard_normal(shape) * 0.2 + 1, FP16)
    return x, w, np.asarray(rng.standard_normal(shape) * 0.2, FP16)


def ln_ref(x, w, b, eps=1e-5):
    v = np.asarray(x, np.float64)
    mu = v.mean(-1, keepdims=True)
    var = ((v - mu) ** 2).mean(-1, keepdims=True)
    return (v - mu) / np.sqrt(var + eps) * np.float64(w) + np.float64(b)


def sm_ref(x):
    v = np.asarray(x, np.float64)
    e = np.exp(v - v.max(-1, keepdims=True))
    return e / e.sum(-1, keepdims=True)


# --------------------------------------------------------------- LayerNorm
@pytest.mark.parametrize("cols", WIDTHS)
def test_api_layernorm_runs_at_every_sdxl_width(cols):
    """`api.layernorm` RAISED at 640 and at 1280, which is all 210 of them."""
    m = 16
    x, w, b = operands((m, cols))
    dev = SimDevice(size=1024 << 20)
    api._device = dev
    got = api.layernorm(dev.tensor(x), dev.tensor(w), dev.tensor(b)).numpy()
    grade(got.reshape(m, cols), ln_ref(x, w, b), p99=1e-3)
    assert dev.saturated == 0
    assert dev.counters.get("relayouts", 0) == 0


@pytest.mark.parametrize("cols", WIDTHS)
def test_api_softmax_runs_at_every_sdxl_width(cols):
    m = 16
    x, _, _ = operands((m, cols))
    dev = SimDevice(size=1024 << 20)
    api._device = dev
    got = api.softmax(dev.tensor(x)).numpy()
    grade(got.reshape(m, cols), sm_ref(x), p99=1e-3)
    assert dev.saturated == 0


def test_the_odd_fold_is_the_SAME_ANSWER_as_a_power_of_two_one():
    """A split scheme that lost a sub-row still normalises, to a wrong variance.

    So the check is against the arithmetic, not against a self-consistency the
    dropped sub-row would also satisfy: at 1280 the fold is 8 halvings, a split
    at 4 and a join, and every one of the 10 sub-rows has to reach sub-row 0.
    """
    m = 8
    for cols in (1024, 640, 1280):
        x, w, b = operands((m, cols))
        rows = split(cols)
        dev = SimDevice(size=1024 << 20)
        flat = [a.reshape(-1, 128) for a in (x, w, b)]
        got = layernorm_wide(
            *[dev.tensor(a) for a in flat], rows=rows, part=part_for(cols)
        ).numpy()
        grade(got.reshape(m, cols), ln_ref(x, w, b), p99=1e-3)


def test_the_fold_shrinks_by_EXACTLY_rows_minus_one_however_it_splits():
    """The one length the whole scheme is pinned to, from both sides.

    One shorter and the last group's start falls off the end; one longer and a
    level read past its own operand. It is `rows - 1` for a power of two and for
    everything else, which is what makes a spread over the result exact.
    """
    from kohakuaccel.lang import dims, units

    from kohakutpu import lang as L

    R, W = dims("R_f, W_f")
    seen: dict = {}

    @L.kernel
    def probe(x=L.In(..., R, W), y=L.Out(..., R, W), *, rows=5, part=8192):
        held = fold_flat(x.as_rows(128), rows, 128, part)
        seen["rows"] = held.rows
        with units(y.as_rows(128).parts(part)) as e:
            y.as_rows(128)[e] <<= held.rows_from(0)[e] * 1.0

    for rows in (2, 4, 5, 8, 10, 16, 20):
        dev = SimDevice(size=256 << 20)
        zero = np.zeros((4 * rows, 128), FP16)
        probe.plan(dev.tensor(zero), rows=rows, part=part_for(rows * 128))
        held = int(seen["rows"].resolve({"R_f": 4 * rows, "W_f": 128}))
        assert held == 4 * rows - (rows - 1), rows


# --------------------------------------------------------------- GroupNorm
@pytest.mark.parametrize("group", GROUPS)
@pytest.mark.parametrize("eps", [1e-5, 1e-6])
def test_group_norm_wide_at_the_three_sdxl_group_sizes(group, eps):
    """34 resnet norms at eps 1e-5 and 11 transformer ones at 1e-6."""
    x, w, b = operands((2, group))
    dev = SimDevice(size=4096 << 20)
    flat = [a.reshape(-1, 128) for a in (x, w, b)]
    got = group_norm_wide(
        *[dev.tensor(a) for a in flat],
        eps=eps,
        rows=split(group),
        part=part_for(group),
    ).numpy()
    grade(got.reshape(2, group), ln_ref(x, w, b, eps), p99=1e-3)
    assert dev.saturated == 0


def test_group_norm_wide_holds_up_at_a_mean_far_OFF_zero():
    """What `E[x^2] - E[x]^2` cannot do: at 64 sigma it is 21.5% low.

    Two passes over the group is the reason, and this is the shape that tells
    the two apart -- a centred operand does not.
    """
    group = GROUPS[0]
    x, w, b = operands((2, group), shift=64.0)
    dev = SimDevice(size=4096 << 20)
    flat = [a.reshape(-1, 128) for a in (x, w, b)]
    got = group_norm_wide(
        *[dev.tensor(a) for a in flat], rows=split(group), part=part_for(group)
    ).numpy()
    grade(got.reshape(2, group), ln_ref(x, w, b), p99=1e-2)


@pytest.mark.parametrize("group", GROUPS)
def test_group_norm_silu_is_the_pair_every_resnet_issues(group):
    """`norm -> act -> conv`, so the pair that touches is the norm and the act."""
    x, w, b = operands((2, group))
    dev = SimDevice(size=4096 << 20)
    flat = [a.reshape(-1, 128) for a in (x, w, b)]
    got = group_norm_silu_wide(
        *[dev.tensor(a) for a in flat], rows=split(group), part=part_for(group)
    ).numpy()
    h = ln_ref(x, w, b)
    grade(got.reshape(2, group), h / (1.0 + np.exp(-h)), p99=1e-3)
    assert dev.saturated == 0


# ----------------------------------------------------------------- softmax
def test_softmax_over_a_77_token_context_masks_its_padding():
    """SDXL's context is 77 and `VRED` folds a multiple of 16; 77 is neither."""
    m, keys, width = 16, 77, 128
    raw, _, _ = operands((m, keys))
    padded = np.zeros((m, width), FP16)
    padded[:, :keys] = raw
    dev = SimDevice(size=512 << 20)
    api._device = dev
    got = api.softmax(dev.tensor(padded), keys=keys).numpy().reshape(m, width)
    grade(got[:, :keys], sm_ref(raw), p99=1e-3)
    assert (got[:, keys:] == 0).all(), "the padded keys carry weight"
    assert abs(1 - got[:, :keys].astype(np.float64).sum(1)).max() < 1e-3


def test_a_pad_the_mask_would_have_to_CANCEL_is_still_exact():
    """The shape an arithmetic correction cannot do, and the reason for a mask.

    Subtracting `(width - keys) * exp(-max)` from the sum is exact in real
    arithmetic and catastrophic in fp16 when the real scores sit far below the
    zero pad: at a shift of -10 the correction is 99.99% of the sum.
    """
    m, keys, width = 16, 77, 128
    raw, _, _ = operands((m, keys), shift=-10.0, seed=3)
    padded = np.zeros((m, width), FP16)
    padded[:, :keys] = raw
    dev = SimDevice(size=512 << 20)
    api._device = dev
    got = api.softmax(dev.tensor(padded), keys=keys).numpy().reshape(m, width)
    grade(got[:, :keys], sm_ref(raw), p99=1e-3)


def test_the_refusal_at_77_NAMES_the_padding_and_the_knob():
    """A refusal that does not say `keys=` costs the next reader the derivation."""
    dev = SimDevice(size=64 << 20)
    api._device = dev
    with pytest.raises(ValueError, match="keys="):
        api.softmax(dev.tensor(np.zeros((4, 77), FP16)))


def test_keys_is_refused_past_one_reduction_pass():
    """`keys` masks ONE VRED pass; a wider row folds hierarchically instead."""
    dev = SimDevice(size=64 << 20)
    api._device = dev
    with pytest.raises(ValueError, match="not one VRED pass"):
        api.softmax(dev.tensor(np.zeros((4, 640), FP16)), keys=600)


def test_the_edge_mask_is_ONE_ROW_read_at_stride_zero():
    """It was the row's FULL shape while `Buffer.repeated` served one instance.

    A table is uploaded per compilation, so a full-shape mask cost as much as
    the tensor it masked. Pinned as a SIZE because the answers are identical
    either way -- only the constant moved.
    """
    m, keys, width = 64, 77, 128
    padded = np.zeros((m, width), FP16)
    padded[:, :keys] = operands((m, keys))[0]
    dev = SimDevice(size=512 << 20)
    api._device = dev
    plan = _o.softmax_keys.plan(
        dev.tensor(padded), keys=keys, width=width, part=part_for(width)
    )
    sizes = [int(np.asarray(a).size) for a in plan.tables.values()]
    assert sizes == [width], plan.tables
    assert width < m * width


def test_softmax_at_the_VAE_mid_attention_width():
    """16,384 keys, one head: the heaviest single operator in the pipeline."""
    m, cols = 2, 16384
    x, _, _ = operands((m, cols))
    dev = SimDevice(size=2048 << 20)
    api._device = dev
    got = api.softmax(dev.tensor(x)).numpy()
    grade(got.reshape(m, cols), sm_ref(x), p99=1e-3)


def test_softmax_wide_still_refuses_a_part_that_CUTS_a_group():
    """`part_for` exists because 1280 does not divide the kernel's own 8192."""
    dev = SimDevice(size=256 << 20)
    with pytest.raises(ValueError, match="whole"):
        softmax_wide(dev.tensor(np.zeros((16, 1280), FP16)), rows=10, part=8192)
    assert part_for(1280) == 7680
    assert part_for(1024) == 8192
    assert part_for(163840) == 163840
