"""A PyTorch-shaped module system whose tensors are tinygrad's.

`Module` walks `__dict__` for children and parameters rather than keeping
registries, so assigning one in `__init__` is all it takes to register it --
same as torch, and it keeps a layer's source the length of its arithmetic.

Parameters arrive as numpy and become device tensors on `load_state_dict`, so a
module is constructible without a checkpoint and printable before one exists.
"""

import ktpugrad
import numpy as np
from kohakutpu.hw.vector import VLMAX
from tinygrad.tensor import Tensor

#: Every tensor on this machine is fp16; the accumulator's width is invisible
#: here and the reference in `reference.py` is what fp32 comparison happens in.
DTYPE = np.float16


class Parameter:
    """A named weight: numpy until loaded, a device Tensor after.

    `shape` is what the checkpoint must supply and is checked on load, because a
    silently transposed weight produces a plausible wrong image rather than an
    error.
    """

    def __init__(self, *shape: int, transposed: bool = False) -> None:
        self.shape = tuple(shape)
        self.transposed = transposed
        self.tensor: Tensor | None = None

    def load(self, array: np.ndarray, where: str) -> None:
        """Put `array` on the device. Raises ValueError on a shape mismatch.

        `transposed` flips it HERE, once, because the device has no transpose:
        a `.T` view is a strided load the chain generator refuses.
        """
        if tuple(array.shape) != self.shape:
            raise ValueError(
                f"{where}: checkpoint has {array.shape}, want {self.shape}"
            )
        held = array.T if self.transposed else array
        self.tensor = Tensor(np.ascontiguousarray(held, DTYPE), device=ktpugrad.NAME)

    def __call__(self) -> Tensor:
        """The device tensor. Raises RuntimeError before `load`."""
        if self.tensor is None:
            raise RuntimeError(
                "this parameter has no weights yet; load_state_dict first"
            )
        return self.tensor


class Module:
    """Owns parameters and child modules, and runs on `__call__`.

    Subclasses assign `Parameter`s and `Module`s in `__init__` and define
    `forward`. Nothing else is required.
    """

    def forward(self, *args, **kwargs):
        raise NotImplementedError(f"{type(self).__name__} defines no forward")

    def __call__(self, *args, **kwargs):
        return self.forward(*args, **kwargs)

    def children(self):
        """`(name, module)` for every child, in assignment order."""
        for name, value in vars(self).items():
            if isinstance(value, Module):
                yield name, value
            elif isinstance(value, (list, tuple)):
                for i, item in enumerate(value):
                    if isinstance(item, Module):
                        yield f"{name}.{i}", item

    def named_parameters(self, prefix: str = ""):
        """`(dotted name, Parameter)` over this module and everything under it."""
        for name, value in vars(self).items():
            if isinstance(value, Parameter):
                yield f"{prefix}{name}", value
        for name, child in self.children():
            yield from child.named_parameters(f"{prefix}{name}.")

    def load_state_dict(self, store: dict, prefix: str = "") -> list[str]:
        """Load every parameter from `store` by name; returns the names missing.

        Missing names are RETURNED rather than raised, so a partially converted
        checkpoint says which layers it cannot fill instead of stopping at the
        first one.
        """
        absent = []
        for name, param in self.named_parameters(prefix):
            if name in store:
                param.load(store[name], name)
            else:
                absent.append(name)
        return absent

    def nbytes(self) -> int:
        """Bytes of parameter this module and its children hold."""
        return sum(
            int(np.prod(p.shape)) * np.dtype(DTYPE).itemsize
            for _, p in self.named_parameters()
        )


#: Host round trips this forward pass has made, by what forced each one. A
#: relayout here is a REAL copy through numpy, not a counter.
RELAYOUTS: dict[str, int] = {}


def relayout(why: str, x: Tensor, permute: tuple) -> Tensor:
    """Permute `x` on the HOST and send it back, recording that it happened.

    The chain generator lowers broadcasts, not arbitrary strided permutes, so a
    head split refuses on device: "a load with strides ... is not a broadcast".
    Doing it here makes the cost visible instead of hiding it in a refusal.
    """
    RELAYOUTS[why] = RELAYOUTS.get(why, 0) + 1
    moved = np.ascontiguousarray(np.transpose(x.numpy(), permute), DTYPE)
    return Tensor(moved, device=ktpugrad.NAME)


def chunk2(why: str, x: Tensor, width: int) -> tuple[Tensor, Tensor]:
    """Split `x` in two along the last axis, on the HOST, and record it.

    A slice is a strided view and the device cannot materialise one, so the
    copy kernel refuses. Counted like any other relayout.
    """
    RELAYOUTS[why] = RELAYOUTS.get(why, 0) + 1
    held = x.numpy()
    return (
        Tensor(np.ascontiguousarray(held[..., :width], DTYPE), device=ktpugrad.NAME),
        Tensor(np.ascontiguousarray(held[..., width:], DTYPE), device=ktpugrad.NAME),
    )


def _wide(x: Tensor, w: Tensor, b: Tensor, dim: int, eps: float) -> Tensor:
    """`layernorm_wide` over a row wider than VLMAX, through the bridge."""
    from kohakutpu.kernels import wide

    rows = wide.split(dim)
    fn = ktpugrad.bridge(wide.layernorm_wide, spread=("w", "b"))
    flat = x.reshape(-1, dim).contiguous()
    return fn(flat, w, b, eps=eps, rows=rows).reshape(*x.shape)


class Linear(Module):
    """`x @ w.T + b`, with `w` stored `[out][in]` as torch keeps it."""

    def __init__(self, dim_in: int, dim_out: int, bias: bool = True) -> None:
        self.weight = Parameter(dim_out, dim_in)
        self.bias = Parameter(dim_out) if bias else None

    def forward(self, x: Tensor) -> Tensor:
        # FLATTENED to `[rows, K]` first: a rank-3 operand schedules as a rank-4
        # reduce, which the matcher declines as an ambiguous batched fold.
        lead, k = x.shape[:-1], x.shape[-1]
        flat = x.reshape(-1, k).contiguous()
        # `w.T` as a VIEW, which is the spelling the matcher reads: `w` is
        # `[N][K]` and the contraction is the last axis of both.
        # REALISED before the bias: left lazy, the add fuses into the matmul and
        # the scheduler emits a rank-3 walk with a cast the matcher declines.
        out = (flat @ self.weight().T).contiguous().realize()
        if self.bias is not None:
            # FULL SHAPE, not per channel: a staged epilogue runs after the
            # tiling, where a per-channel operand has no tile index left.
            wide = self.bias().reshape(1, -1).expand(out.shape).contiguous()
            out = out + wide
        return out.reshape(*lead, out.shape[-1])


class LayerNorm(Module):
    """Normalise the last axis, then scale and shift.

    Dispatches to `ktpugrad.layernorm`, one kernel against tinygrad's three.
    """

    def __init__(self, dim: int, eps: float = 1e-5) -> None:
        self.dim, self.eps = dim, eps
        self.weight, self.bias = Parameter(dim), Parameter(dim)

    def forward(self, x: Tensor) -> Tensor:
        if self.dim <= VLMAX:
            return ktpugrad.layernorm(x, self.weight(), self.bias(), eps=self.eps)
        # A row past VLMAX folds HIERARCHICALLY: `layernorm_wide` takes it as
        # `rows * width` with `rows` from `split`, which is what SDXL needs.
        return _wide(x, self.weight(), self.bias(), self.dim, self.eps)


class GroupNorm(Module):
    """GroupNorm over `[N, C, H, W]`, as the UNet's resnet blocks use it.

    A group is a ROW once the activation is reshaped to `[N*G, C/G*H*W]`, which
    is the shape `kernels.group_norm` wants and why the reshape is here.
    """

    def __init__(self, groups: int, channels: int, eps: float = 1e-5) -> None:
        self.groups, self.channels, self.eps = groups, channels, eps
        self.weight, self.bias = Parameter(channels), Parameter(channels)

    def forward(self, x: Tensor) -> Tensor:
        n, c, h, w = x.shape
        rows = x.reshape(n * self.groups, c // self.groups * h * w)
        gain = self.weight().reshape(self.channels, 1).expand(self.channels, h * w)
        shift = self.bias().reshape(self.channels, 1).expand(self.channels, h * w)
        out = ktpugrad.layernorm(
            rows,
            gain.reshape(rows.shape),
            shift.reshape(rows.shape),
            eps=self.eps,
        )
        return out.reshape(n, c, h, w)


class SiLU(Module):
    """`x * sigmoid(x)`, as one bridged kernel rather than four tinygrad ops."""

    def forward(self, x: Tensor) -> Tensor:
        from sdxl.kernels import silu

        return silu(x)
