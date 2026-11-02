"""Output statistics and the simulated MXFP7 reference they compare the card with."""

from types import SimpleNamespace as NS

import numpy as np
from examples.tinygrad import vit_tiny
from tinygrad.tensor import Tensor

from kohakutpu.application.checks import accuracy as A
from kohakutpu.language.numerics import mxfp7

R = np.random.default_rng(3)


def quantised(a):
    """FP16 then MXFP7 along the last axis, zero-padded to whole blocks."""
    k = a.shape[-1]
    pad = [(0, 0)] * (a.ndim - 1) + [(0, -k % mxfp7.KBLOCK)]
    return mxfp7.value_fp16(np.pad(a, pad))[..., :k]


def test_only_a_value_beyond_the_formats_largest_error_is_listed():
    f32 = R.normal(size=(4, 10))
    mx = f32 + R.uniform(-0.004, 0.004, size=f32.shape)
    mx[1, 1] = f32[1, 1] + 0.01
    card = mx.copy()
    card[2, 7] += 0.5
    # Past the format's largest error from f32 but on the MXFP7 value: not listed.
    card[1, 1] += 1e-6
    # Format-sized below f32, past the largest error from the MXFP7 value: not listed.
    mx[0, 0] = f32[0, 0] + 0.004
    card[0, 0] = f32[0, 0] - 0.008

    report = A.output_report(card, f32, mx)

    assert report["outside_footprint"] == 1
    assert report["worst"][0]["at"] == [2, 7]
    assert report["worst"][0]["mxfp7"] == mx[2, 7]
    assert report["card_mxfp7"]["absolute"]["max"] == 0.5
    assert report["card_mxfp7"]["absolute"]["median"] == 0.0
    footprint = np.abs(mx - f32)
    assert report["mxfp7_f32"]["absolute"]["mean"] == footprint.mean()
    assert report["mxfp7_f32"]["relative"]["median"] == np.median(
        footprint / np.abs(f32)
    )


def test_a_matmul_reads_both_operands_in_mxfp7_along_the_reduction():
    a = R.normal(size=(2, 8, 64)).astype(np.float32)
    b = R.normal(size=(64, 24)).astype(np.float32) / 8

    with A.mxfp7_operands():
        got = (Tensor(a, device="CPU") @ Tensor(b, device="CPU")).numpy()
    plain = (Tensor(a, device="CPU") @ Tensor(b, device="CPU")).numpy()

    want = np.stack([mxfp7.model_matmul(x, b.T) for x in a])
    np.testing.assert_allclose(got, want, rtol=1e-5, atol=1e-5)
    assert np.abs(plain - want).max() > 100 * np.abs(got - want).max()


def test_a_configurations_references_are_computed_once_then_read(tmp_path):
    calls = []
    weight = Tensor(R.normal(size=(64, 3)).astype(np.float32), device="CPU")

    def host(x):
        calls.append(1)
        return x @ weight

    inputs = [R.normal(size=(1, 64)).astype(np.float32) for _ in range(2)]
    args = NS(references=tmp_path, width=8, blocks=1, image_size=16, seed=3)
    args.batch, args.inputs = 1, 2

    want, mx = vit_tiny.references(host, inputs, args)
    again = vit_tiny.references(host, inputs, args)
    vit_tiny.references(host, inputs, NS(**{**vars(args), "seed": 4}))

    assert len(calls) == 8
    np.testing.assert_array_equal(again[0], want)
    np.testing.assert_array_equal(again[1], mx)
    assert not np.array_equal(want, mx)


def test_a_convolution_reduces_over_channels_then_taps_in_mxfp7():
    x = R.normal(size=(2, 4, 9, 9)).astype(np.float32)
    w = R.normal(size=(6, 2, 3, 3)).astype(np.float32) / 4
    stride, pad, groups = 2, 1, 2

    with A.mxfp7_operands():
        got = Tensor(x, device="CPU").conv2d(
            Tensor(w, device="CPU"), groups=groups, stride=stride, padding=pad
        )
        got = got.numpy()

    padded = np.pad(x, ((0, 0), (0, 0), (pad, pad), (pad, pad)))
    size = (padded.shape[-1] - 3) // stride + 1
    want = np.zeros((2, 6, size, size))
    for g in range(groups):
        wq = quantised(w[3 * g : 3 * g + 3].reshape(3, -1))
        for i in range(size):
            for j in range(size):
                window = padded[:, 2 * g : 2 * g + 2, i * stride :, j * stride :]
                patch = quantised(window[:, :, :3, :3].reshape(2, -1))
                want[:, 3 * g : 3 * g + 3, i, j] = patch @ wq.T
    np.testing.assert_allclose(got, want, rtol=1e-5, atol=1e-5)
