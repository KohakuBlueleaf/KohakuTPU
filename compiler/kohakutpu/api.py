"""The tensor level: what to compute, on arrays that live on the card.

Level 5. An op is a call and a result is a tensor; the only sub-level things
here are the three device controls. Every op is one kernel from
:mod:`kohakutpu.ops`, or a fused one from :mod:`kohakutpu.kernels` where the
work is worth fusing.
"""

import os

import numpy as np
from kohakuaccel.lang import dims as _dims
from kohakutpu.lang import VLMAX as _VLMAX
from kohakutpu.rt import Device, Tensor

from kohakutpu import kernels as _k
from kohakutpu import ops as _o

LOG2E = _k.LOG2E

#: Re-exported so a kernel written beside an application needs one import.
dims = _dims

#: Set to `card` to reach the hardware without saying so at every call site.
DEVICE_ENV = "KOHAKUTPU_DEVICE"
TARGETS = ("card", "sim")

#: What opens when nothing asks. NOT the card: reaching a shared card unasked
#: corrupts whoever is measuring, and neither side can tell it happened.
DEFAULT_TARGET = "sim"

_device: Device | None = None


class Array(Tensor):
    """A device tensor and everything you can compute with it.

    Level 5, on level 2's storage: :class:`kohakutpu.rt.Tensor` knows where the
    bytes are and this knows what they MEAN. Every operator is one kernel from
    :mod:`kohakutpu.ops` -- there is no second implementation path.
    """

    #: So numpy defers `array * tensor` here instead of broadcasting over it.
    __array_ufunc__ = None

    def _operand(self, other) -> "Array":
        """`other` as a tensor of this shape, uploading a scalar as a whole one.

        An elementwise pass walks two operands of ONE length, so a scalar has
        to be materialised rather than folded. Raises :class:`TypeError` for
        anything that is neither a tensor nor a number.
        """
        if isinstance(other, Tensor):
            return other
        if isinstance(other, (int, float, np.floating, np.integer)):
            return self.dev.tensor(np.full(self.shape, other, np.float16))
        raise TypeError(
            f"a tensor operates on another tensor or on a number, not on "
            f"{type(other).__name__}; put an array on the device with "
            f"dev.tensor(...) first"
        )

    def __matmul__(self, other: "Array") -> "Array":
        """``self @ other.T``: the last axis of both operands is contracted."""
        return _o.matmul(self, other)

    def __add__(self, other) -> "Array":
        return _o.residual(self, self._operand(other))

    __radd__ = __add__

    def __mul__(self, other) -> "Array":
        return _o.mul(self, self._operand(other))

    __rmul__ = __mul__

    def __sub__(self, other) -> "Array":
        return _o.sub(self, self._operand(other))

    def __rsub__(self, other) -> "Array":
        return _o.sub(self._operand(other), self)

    def __truediv__(self, other) -> "Array":
        return _o.div(self, self._operand(other))

    def __rtruediv__(self, other) -> "Array":
        return _o.div(self._operand(other), self)

    def __neg__(self) -> "Array":
        return _o.neg(self)

    def silu(self) -> "Array":
        return _o.silu(self)

    def gelu(self) -> "Array":
        return _o.gelu(self)

    def relu(self) -> "Array":
        return _o.relu(self)

    # Through the module functions, not the kernels: the width dispatch lives
    # there, and two entry points would let one of them pick the wrong path.
    def softmax(self) -> "Array":
        return softmax(self)

    def rmsnorm(self, weight: "Array") -> "Array":
        return rmsnorm(self, weight)

    def layernorm(self, weight: "Array", bias: "Array") -> "Array":
        return layernorm(self, weight, bias)


def device(card=None, kind: str | None = None) -> Device:
    """The device tensors land on, opened the first time it is asked.

    `kind` is ``card`` or ``sim``, defaulting to ``$KOHAKUTPU_DEVICE`` and then
    to :data:`DEFAULT_TARGET`, which is the simulator. Passing a `card` object
    is itself a request for hardware. `sim` is `kohakutpu.model.SimDevice`: the
    same kernels through the same artifact, run by the unit models.

    Raises :class:`ValueError` for an unknown `kind`, and propagates whatever
    went wrong opening a card -- the models are never substituted for one.
    """
    global _device
    if _device is None or card is not None or kind is not None:
        _device = _open(kind, card)
    return _device


def add_device_flag(parser) -> None:
    """Give `parser` the `--device` every demo and example takes."""
    parser.add_argument(
        "--device",
        choices=TARGETS,
        default=None,
        help=f"where to run; defaults to {DEFAULT_TARGET}, or ${DEVICE_ENV}",
    )


def script_device(kind: str | None = None) -> Device:
    """The device a demo or example runs on, from its `--device` or None.

    Returns the opened device and makes it the one every later call lands on.
    Raises :class:`ValueError` for an unknown `kind`.
    """
    return device(kind=kind)


def target(kind: str | None = None) -> str:
    """Which of :data:`TARGETS` a request means: `kind`, the environment, sim.

    Raises :class:`ValueError` naming the choices for anything else.
    """
    want = kind or os.environ.get(DEVICE_ENV) or DEFAULT_TARGET
    if want not in TARGETS:
        raise ValueError(f"device kind must be one of {TARGETS}, not {want!r}")
    return want


def _open(kind: str | None, card) -> Device:
    """Open one target, never substituting the other for it.

    A card that will not open raises: silently handing back the models instead
    would report numbers from a simulation as if they came off the silicon.
    """
    # An actual card object is a request for hardware however `kind` was left.
    if kind is None and card is not None:
        kind = "card"
    if target(kind) == "card":
        return Device(card)
    # Imported here: a script that never asks for the models should not pay for
    # importing them, and the card path should not depend on them at all.
    from kohakutpu.model import SimDevice

    return SimDevice()


def tensor(array) -> Tensor:
    """A device tensor holding `array`. Nothing crosses the link until it must."""
    return device().tensor(array)


def zeros(shape) -> Tensor:
    """A device tensor of `shape`, all zero."""
    return tensor(np.zeros(shape, np.float16))


def full(shape, value: float) -> Tensor:
    """A device tensor of `shape`, every element `value`."""
    return tensor(np.full(shape, value, np.float16))


# ------------------------------------------------------------------ device control
def sync() -> None:
    """Wait for outstanding work."""
    device().sync()


def empty_cache() -> None:
    """Return memory nothing is using. Live tensors keep theirs."""
    device().empty_cache()


def stats() -> dict:
    """What is resident and what is free."""
    return device().stats()


# ------------------------------------------------------------------------ compute
def matmul(a: Tensor, b: Tensor, **tiling) -> Tensor:
    """``a @ b.T``. `b` is ``[N][K]``, as a torch Linear weight already is."""
    return _o.matmul(a, b, **tiling)


def bmm(a: Tensor, b: Tensor, **tiling) -> Tensor:
    """``a @ b.T`` with `a` of any leading rank and `b` shared across it.

    The SAME kernel as :func:`matmul`, whose `a` is declared `(..., M, K)` and
    so already takes a batch. Kept as a name people reach for.
    """
    return _o.matmul(a, b, **tiling)


def linear_silu(x: Tensor, w: Tensor, **tiling) -> Tensor:
    """``silu(x @ w.T)`` as ONE kernel: the activation never leaves the card."""
    return _k.linear_silu(x, w, **tiling)


def silu(x: Tensor, **tiling) -> Tensor:
    """``x * sigmoid(x)``, at any rank."""
    return _o.silu(x, **tiling)


def gelu(x: Tensor, **tiling) -> Tensor:
    """``x * sigmoid(1.702 x)``, at any rank."""
    return _o.gelu(x, **tiling)


def relu(x: Tensor, **tiling) -> Tensor:
    """``max(x, 0)``, at any rank."""
    return _o.relu(x, **tiling)


def _fold(x: Tensor, width: int = _VLMAX):
    """Sub-rows this row folds into, or None when it fits ONE reduction pass.

    The dispatch a kernel cannot make: while tracing, an extent is a symbol and
    `x.cols > width` raises `TypeError`. Here the shape is a concrete tuple.
    """
    cols = int(x.shape[-1])
    return None if cols <= width else _k.split(cols, width)


def softmax(x: Tensor, **tiling) -> Tensor:
    """Row-wise softmax, at ANY row width.

    `VRED` folds at most VLMAX lanes, so a wider row goes to the hierarchical
    kernel. Which one is not the caller's problem -- a DiT's 1024-wide row and
    a 64-wide one are the same call.
    """
    rows = _fold(x)
    if rows is None:
        return _o.softmax(x, **tiling)
    return _k.softmax_wide(x, rows=rows, **tiling)


def rmsnorm(x: Tensor, w: Tensor, **tiling) -> Tensor:
    """``x * rsqrt(mean(x^2) + eps) * w``, per row, at any row width."""
    rows = _fold(x)
    if rows is None:
        return _k.rmsnorm(x, w, **tiling)
    return _k.rmsnorm_wide(x, w, rows=rows, **tiling)


def layernorm(x: Tensor, w: Tensor, b: Tensor, **tiling) -> Tensor:
    """``(x - mean) * rsqrt(var + eps) * w + b``, per row, at any row width."""
    rows = _fold(x)
    if rows is None:
        return _k.layernorm(x, w, b, **tiling)
    return _k.layernorm_wide(x, w, b, rows=rows, **tiling)


def neg(x: Tensor) -> Tensor:
    """``-x``, at any rank."""
    return _o.neg(x)


def abs(x: Tensor) -> Tensor:
    """``|x|``, at any rank."""
    return _o.absolute(x)


def exp2(x: Tensor) -> Tensor:
    """``2**x``, at any rank."""
    return _o.exp2(x)


def log2(x: Tensor) -> Tensor:
    """``log2(x)``, at any rank."""
    return _o.log2(x)


def recip(x: Tensor) -> Tensor:
    """``1 / x``, at any rank."""
    return _o.recip(x)


def rsqrt(x: Tensor) -> Tensor:
    """``1 / sqrt(x)``, at any rank."""
    return _o.rsqrt(x)


def add(x: Tensor, y: Tensor) -> Tensor:
    """``x + y``, at any rank."""
    return _o.residual(x, y)


def sub(x: Tensor, y: Tensor) -> Tensor:
    """``x - y``, at any rank."""
    return _o.sub(x, y)


def mul(x: Tensor, y: Tensor) -> Tensor:
    """``x * y``, at any rank."""
    return _o.mul(x, y)


def div(x: Tensor, y: Tensor) -> Tensor:
    """``x / y``, at any rank."""
    return _o.div(x, y)


def row_sum(x: Tensor) -> Tensor:
    """Sum each row, broadcast back across it."""
    return _o.row_sum(x)


def row_max(x: Tensor) -> Tensor:
    """Maximum of each row, broadcast back across it."""
    return _o.row_max(x)
