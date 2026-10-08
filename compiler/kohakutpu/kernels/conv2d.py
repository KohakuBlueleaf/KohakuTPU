"""3x3 convolution with its bias folded in.

A bias ALWAYS follows a conv, so this is structural rather than a guessed
fusion. The plain conv is an op -- `kohakutpu.ops.conv2d` (conv2d.md §6).
"""

from kohakutpu.ops.conv2d import conv2d
from kohakutpu.ops.elementwise import residual


def conv2d_bias(a, b, bias, *, gm=16, gn=32):
    """``conv2d(a, b) + bias``, `bias` at the result's full ``[rows][N]`` shape."""
    return residual(conv2d(a, b, gm=gm, gn=gn), bias)
