"""Output error statistics: the card against CPU f32, and against the same CPU model
with every matrix operand read as the clusters read it, FP16 then MXFP7.

The MXFP7 run (`mxfp7_operands`) keeps f32 everywhere else, so its distance from f32 is
the format's footprint on that output. Only card values further than the footprint's
largest error from both f32 and the MXFP7 run are listed for examination.
"""

import contextlib
import math
from collections.abc import Sequence

import numpy as np
from tinygrad.dtype import DTypeLike
from tinygrad.tensor import Tensor

from kohakutpu.language.numerics import mxfp7

RELATIVE_FLOOR = 1e-9
DEFAULT_INPUTS = 48
PER_IMAGE_SECONDS = 1.24
#: Values outside the footprint listed, largest error first.
WORST = 8


def summary(values) -> dict:
    v = np.asarray(values, np.float64)
    return dict(max=float(v.max()), mean=float(v.mean()), median=float(np.median(v)))


def error_stats(got, want) -> dict:
    """Absolute and elementwise relative error, max/mean/median over every element."""
    g, w = np.asarray(got, np.float64), np.asarray(want, np.float64)
    absolute = np.abs(g - w)
    relative = absolute / np.maximum(np.abs(w), RELATIVE_FLOOR)
    return dict(absolute=summary(absolute), relative=summary(relative))


def output_report(got, f32, mx) -> dict:
    """`got` (the card) against `f32` and `mx` (the MXFP7 run); the last axis is the
    class axis of top-1."""
    g, w, m = (np.asarray(a, np.float64) for a in (got, f32, mx))
    error = np.minimum(np.abs(g - w), np.abs(g - m))
    footprint = float(np.abs(m - w).max())

    outside = np.argwhere(error > footprint)
    worst = outside[np.argsort(-error[tuple(outside.T)], kind="stable")][:WORST]
    return dict(
        card_f32=error_stats(g, w),
        card_mxfp7=error_stats(g, m),
        mxfp7_f32=error_stats(m, w),
        elements=int(g.size),
        outside_footprint=len(outside),
        worst=[
            dict(
                at=[int(i) for i in at],
                card=float(g[tuple(at)]),
                f32=float(w[tuple(at)]),
                mxfp7=float(m[tuple(at)]),
            )
            for at in worst
        ],
        top1_f32=float((g.argmax(-1) == w.argmax(-1)).mean()),
        top1_mxfp7=float((g.argmax(-1) == m.argmax(-1)).mean()),
    )


def operand(a, axis: int) -> np.ndarray:
    """`a` as a cluster reads it: FP16 in memory, MXFP7 in blocks of 32 along `axis`
    (the reduction), zero-padded to a whole block."""
    moved = np.moveaxis(np.asarray(a, np.float32), axis, -1)
    k = moved.shape[-1]
    pad = [(0, 0)] * (moved.ndim - 1) + [(0, -k % mxfp7.KBLOCK)]
    value = mxfp7.value_fp16(np.pad(moved, pad))[..., :k]
    return np.moveaxis(value, -1, axis).astype(np.float32)


@contextlib.contextmanager
def mxfp7_operands():
    """CPU tinygrad whose `dot` and `conv2d` read both operands through `operand`.

    A convolution reduces over (input channel, kernel taps) in that order: its windows
    come from the plain convolution with an identity kernel, so padding, stride and
    dilation are tinygrad's own. An operand already realized when it is read (a
    weight, after the f32 run) is quantised once for the whole context."""
    dot, conv2d = Tensor.dot, Tensor.conv2d
    cache = {}

    def read(t: Tensor, shape: tuple[int, ...], axis: int) -> np.ndarray:
        if not t.uop.base.is_realized:
            return operand(t.numpy().reshape(shape), axis)
        key = (t.uop, shape, axis)
        if key not in cache:
            cache[key] = operand(t.numpy().reshape(shape), axis)
        return cache[key]

    def quantised_dot(
        self: Tensor, w: Tensor, dtype: DTypeLike | None = None
    ) -> Tensor:
        x = read(self, tuple(int(s) for s in self.shape), -1)
        y = read(w, tuple(int(s) for s in w.shape), -min(w.ndim, 2))
        result = Tensor(np.matmul(x, y), device=self.device)
        return result if dtype is None else result.cast(dtype)

    def quantised_conv2d(
        self: Tensor,
        weight: Tensor,
        bias: Tensor | None = None,
        groups: int = 1,
        stride: int = 1,
        dilation: int = 1,
        padding: int | Sequence[int] = 0,
        dtype: DTypeLike | None = None,
    ) -> Tensor:
        cout, cin, *taps = (int(s) for s in weight.shape)
        channels = cin * groups
        size = math.prod(taps)
        eye = np.tile(np.eye(size, dtype=np.float32), (channels, 1))
        identity = Tensor(eye.reshape(-1, 1, *taps), device=self.device)
        windows = conv2d(
            self,
            identity,
            groups=channels,
            stride=stride,
            dilation=dilation,
            padding=padding,
        ).numpy()
        batch, _, *out = windows.shape

        x = operand(windows.reshape(batch, groups, cin * size, -1), 2)
        w = read(weight, (groups, cout // groups, cin * size), -1)
        y = np.einsum("bgkp,gok->bgop", x, w).reshape(batch, cout, *out)
        result = Tensor(y, device=self.device)
        if dtype is not None:
            result = result.cast(dtype)
        return result if bias is None else result + bias.reshape(1, -1, *[1] * len(out))

    Tensor.dot, Tensor.conv2d = quantised_dot, quantised_conv2d
    try:
        yield
    finally:
        Tensor.dot, Tensor.conv2d = dot, conv2d
