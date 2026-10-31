"""The KohakuTPU tensor API: numpy-like arrays on the card, every op a
generated DSL kernel (chains fused), your own `@kt.kernel` one call away.

    from kohakutpu.application import api as kp
    x = kp.tensor(np.random.randn(256, 128))
    w = kp.tensor(np.random.randn(512, 128) / 11)
    y = kp.linear(x, w).gelu().layernorm()       # 2 kernels: matmul+gelu, layernorm
    y.numpy(); kp.runs()                         # the result; what ran, cycles each

    z = kp.function(my_kernel)(y, eps=1e-6)      # a @kt.kernel on tensors

The device is the runtime's (the Verilated card); docs/projects/kohakutpu/
api.md is the reference.
"""

import numpy as np

from kohakutpu.application.api.custom import call, function
from kohakutpu.application.api.tensor import (
    Tensor,
    Unsupported,
    linear,
    matmul,
    set_fusion,
    tensor,
)
from kohakutpu.application.runtime import device as _D


def zeros(*shape) -> Tensor:
    return tensor(np.zeros(_shape(shape), np.float16))


def ones(*shape) -> Tensor:
    return tensor(np.ones(_shape(shape), np.float16))


def full(shape, value: float) -> Tensor:
    return tensor(np.full(shape, value, np.float16))


def arange(n: int) -> Tensor:
    return tensor(np.arange(n, dtype=np.float16))


def _shape(shape) -> tuple:
    return (
        tuple(shape[0])
        if len(shape) == 1 and isinstance(shape[0], (tuple, list))
        else tuple(shape)
    )


def where(cond: Tensor, a, b) -> Tensor:
    return cond.where(a, b)


def softmax(x: Tensor, axis: int = -1) -> Tensor:
    return x.softmax(axis)


def layernorm(x: Tensor, weight=None, bias=None, eps: float = 1e-5) -> Tensor:
    return x.layernorm(weight, bias, eps)


def rmsnorm(x: Tensor, weight=None, eps: float = 1e-6) -> Tensor:
    return x.rmsnorm(weight, eps)


def device():
    """The runtime device tensors live on."""
    return _D.device()


def runs() -> list:
    """Every kernel run so far: name, card cycles, wall time, what it was."""
    return list(_D.device().runs)


__all__ = [
    "Tensor",
    "Unsupported",
    "arange",
    "call",
    "device",
    "full",
    "function",
    "layernorm",
    "linear",
    "matmul",
    "ones",
    "rmsnorm",
    "runs",
    "set_fusion",
    "softmax",
    "tensor",
    "where",
    "zeros",
]
