"""THE ESCAPE HATCH: calling a library kernel the scheduler would have split.

tinygrad cuts our multi-stage kernels apart before the matcher sees them.
`x.softmax()` is three of its kernels against one of ours, `layernorm` three,
and `Tensor.scaled_dot_product_attention` is **five of which none match** -- so
from that rung attention does not run at all. Re-fusing across those boundaries
is a pass over a SCHEDULE, which is the line `.plan/TINYGRAD.md` §8 stops at.

Each of these REALISES its inputs, calls the kernel, and hands back a tinygrad
Tensor -- one realize boundary, where the scheduler breaks its own plan anyway.

**Nothing here is a fallback.** A scheduled kernel the matcher refuses still
RAISES and names the ops it saw; these are called deliberately, by name.
"""

import numpy as np
from tinygrad import Tensor
from tinygrad.device import Device
from tinygrad.dtype import dtypes

from kohakutpu import kernels as K
from ktpugrad.device import NAME

FP16 = np.float16

#: `kohakutpu.kernels.activation.LOG2E`. `flash_attention` takes `q` already
#: multiplied by it, since `exp2` is the transcendental the core has.
LOG2E = 1.4426950408889634


def softmax(x: Tensor) -> Tensor:
    """Row-wise softmax over the last axis, as ONE kernel instead of three.

    `x` is an fp16 Tensor whose last axis is the row. Returns a Tensor on the
    KTPU device. Raises :class:`ValueError` for any other dtype, and
    :class:`RuntimeError` without `install()`.
    """
    dev = _rt()
    return _back(K.softmax(dev.tensor(_held(x, "x"))))


def rmsnorm(x: Tensor, w: Tensor, *, eps: float = 1e-5) -> Tensor:
    """``x * rsqrt(mean(x^2) + eps) * w``, as ONE kernel instead of two.

    `w` is the gain, per-channel or at full shape; it is spread here, since an
    elementwise pass reads operands of one length. Returns a Tensor on the KTPU
    device. Raises :class:`ValueError` for a non-fp16 operand or a gain that
    does not broadcast to `x`, and :class:`RuntimeError` without `install()`.
    """
    dev = _rt()
    held = _held(x, "x")
    gain = _spread(_held(w, "w"), held.shape, "w")
    return _back(K.rmsnorm(dev.tensor(held), dev.tensor(gain), eps=eps))


def layernorm(x: Tensor, w: Tensor, b: Tensor, *, eps: float = 1e-5) -> Tensor:
    """``(x - mean) * rsqrt(var + eps) * w + b``, as ONE kernel instead of three.

    `w` and `b` are per-channel or at full shape and are spread here. Returns a
    Tensor on the KTPU device. Raises :class:`ValueError` for a non-fp16 operand
    or an affine term that does not broadcast to `x`, and :class:`RuntimeError`
    without `install()`.
    """
    dev = _rt()
    held = _held(x, "x")
    gain = _spread(_held(w, "w"), held.shape, "w")
    shift = _spread(_held(b, "b"), held.shape, "b")
    return _back(
        K.layernorm(dev.tensor(held), dev.tensor(gain), dev.tensor(shift), eps=eps)
    )


def attention(
    q: Tensor,
    k: Tensor,
    v: Tensor,
    *,
    causal: bool = False,
    scale: float | None = None,
    block: int | None = None,
    keys: int | None = None,
) -> Tensor:
    """``softmax(q @ k.T / sqrt(d)) @ v`` through `K.flash_attention`.

    `q`, `k` and `v` are `[..., L, D]` in the ordinary layout, heads on their
    own leading axis. `scale` defaults to `Dqk ** -0.5` and `block` to `Dv`,
    which the kernel requires them to agree on; `causal` and `keys` are the
    kernel's own masks. Returns a Tensor on the KTPU device.

    Raises :class:`ValueError` for a non-fp16 operand, for ranks that disagree,
    or for a sequence that is not a whole number of blocks, and
    :class:`RuntimeError` without `install()`.
    """
    dev = _rt()
    qh, kh, vh = (_held(t, n) for t, n in ((q, "q"), (k, "k"), (v, "v")))
    if not qh.shape[:-2] == kh.shape[:-2] == vh.shape[:-2]:
        raise ValueError(
            f"q{qh.shape}, k{kh.shape} and v{vh.shape} disagree on the leading "
            f"axes; heads and batch ride the ellipsis and must match"
        )
    dv = vh.shape[-1]
    width = dv if block is None else block
    for name, length in (("Lq", qh.shape[-2]), ("Lkv", kh.shape[-2])):
        if length % width:
            raise ValueError(
                f"{name} is {length}, not a whole number of {width}-wide blocks; "
                f"the kernel reads past the operand rather than padding"
            )
    at = (qh.shape[-1] ** -0.5) if scale is None else scale
    # Pre-scaled in fp32 and cast once: the kernel folds `log2 e` into `q`
    # rather than spending a whole vector pass on it.
    scaled = (qh.astype(np.float32) * at * LOG2E).astype(FP16)
    flat = np.ascontiguousarray(np.swapaxes(vh, -1, -2))
    return _back(
        K.flash_attention(
            dev.tensor(scaled),
            dev.tensor(kh),
            dev.tensor(flat),
            block=width,
            causal=causal,
            keys=keys,
        )
    )


def _rt():
    """The KohakuTPU behind the KTPU device.

    Raises :class:`RuntimeError` when the seam is not installed: reading a KTPU
    tensor back schedules a copy through it, and tinygrad's own error names the
    renderer rather than the call that was missed.
    """
    from tinygrad.engine import realize

    from ktpugrad.runner import to_program

    if realize.to_program is not to_program:
        raise RuntimeError(
            "call ktpugrad.install() first: reading a KTPU tensor schedules a "
            "copy through the seam, and without it tinygrad asks the renderer "
            "for a program this backend does not generate"
        )
    return Device[NAME].rt


def _held(t: Tensor, name: str):
    """`t` as a contiguous row-major fp16 host array.

    Raises :class:`ValueError` for any other dtype: this machine carries no
    element type but fp16 and a cast here would hide that.
    """
    if t.dtype is not dtypes.half:
        raise ValueError(
            f"{name} is {t.dtype} and this machine carries only fp16; cast it "
            f"with `.cast(dtypes.half)` where the precision loss is visible"
        )
    return np.ascontiguousarray(t.numpy(), FP16)


def _spread(held, shape: tuple, name: str):
    """A per-channel operand at the full shape a pass reads.

    Raises :class:`ValueError` when it does not broadcast to `shape`.
    """
    try:
        return np.ascontiguousarray(np.broadcast_to(held, shape))
    except ValueError:
        raise ValueError(f"{name}{held.shape} does not broadcast to {shape}") from None


def _back(got) -> Tensor:
    """A KohakuTPU tensor as a tinygrad Tensor on the KTPU device."""
    return Tensor(np.ascontiguousarray(got.numpy(), FP16), device=NAME)
