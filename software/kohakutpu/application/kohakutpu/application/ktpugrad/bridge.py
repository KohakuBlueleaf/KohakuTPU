"""Any DSL kernel as a function of tinygrad Tensors: the way to run your own
kernel where tinygrad's scheduler would not write it.

    silu_t = ktpugrad.bridge(my_silu)          # a @kt.kernel
    y = silu_t(Tensor(x, device="KTPU"))       # Tensors in, a Tensor out

Each input is realised on KTPU and handed over in the form its port asks for
(f16 bytes, or an MXFP7 packing made on the card); each output is a new KTPU
Tensor the kernel writes row-major (``.outputs(...)`` returns them all for a
kernel of several). The kernel is traced once per shape and constant set
(`kernel.specialise`); it composes with tinygrad on both sides.
"""

import numpy as np
from tinygrad.dtype import dtypes
from tinygrad.tensor import Tensor

from kohakutpu.application.ktpugrad.device import NAME
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.storage import FLAT
from kohakutpu.language import dsl as kt

_TINY = {"f16": dtypes.half, "f32": dtypes.float}


def _code(t: Tensor) -> str:
    if t.dtype == dtypes.half:
        return "f16"
    if t.dtype == dtypes.float:
        return "f32"
    raise ValueError(f"a KTPU kernel takes f16 or f32 tensors, not {t.dtype}")


def _realized(ts: tuple) -> tuple:
    """Each of `ts` on KTPU in a buffer of its own, realised in ONE schedule
    (a producer they share, as q, k and v share their projection, runs
    once). `Tensor._buffer` of a lazy tensor realises a temporary freed when
    that call returns, so the caller holds these while the kernel runs."""
    out = tuple((t if t.device == NAME else t.to(NAME)).contiguous() for t in ts)
    if out:
        Tensor.realize(*out)
    return out


def _storage(t: Tensor):
    return t._buffer()._buf


class Bridged:
    """`kernel` over tinygrad Tensors: one per input port in order, the
    constants as keywords. Calling it returns the output Tensor of a
    one-output kernel; `outputs` returns every output, in port order."""

    def __init__(self, kernel: kt.Kernel) -> None:
        self.kernel = kernel
        self.__name__ = kernel.name
        self.__doc__ = (
            f"`{kernel.name}` over tinygrad Tensors: "
            f"{', '.join(kernel.inputs)} -> {', '.join(kernel.outputs)}."
        )

    def __call__(self, *args: Tensor, **consts) -> Tensor:
        if len(self.kernel.outputs) != 1:
            raise ValueError(
                f"{self.kernel.name} writes {self.kernel.outputs}: call .outputs(...)"
            )
        return self.outputs(*args, **consts)[0]

    def outputs(self, *args: Tensor, **consts) -> tuple[Tensor, ...]:
        kernel, ins = self.kernel, self.kernel.inputs
        if len(args) != len(ins):
            raise ValueError(f"{kernel.name} reads {ins}, given {len(args)} tensors")

        rt = D.device()
        args = _realized(args)
        given = {n: kt.TensorSpec.make(t.shape, _code(t)) for n, t in zip(ins, args)}
        compiled = rt.specialise(kernel, given, **consts)
        specs = dict(compiled.params)

        binds = {}
        for n, t in zip(ins, args):
            dt = np.float16 if t.dtype == dtypes.half else np.float32
            binds[n] = FM.for_port(rt, _storage(t), dt, t.shape, specs[n])

        results = []
        for n in kernel.outputs:
            spec = specs[n]
            if spec.dtype not in _TINY or set(spec.layout_dict) - {"node"}:
                raise ValueError(
                    f"{kernel.name}: output {n} is {spec.text()}; a Tensor holds row-major f16 / f32"
                )
            out = Tensor.empty(*spec.shape, dtype=_TINY[spec.dtype], device=NAME)
            st = _storage(out)
            binds[n] = st.addr
            results.append((out, st, spec))

        rt.run(compiled, binds, kind="bridge", info={"kernel": kernel.name})
        for _, st, spec in results:
            st.wrote(FLAT)
            st.dtype = np.dtype(np.float16 if spec.dtype == "f16" else np.float32)
        return tuple(r[0] for r in results)


def bridge(kernel: kt.Kernel) -> Bridged:
    """`kernel` as a function of tinygrad Tensors (`Bridged`)."""
    return Bridged(kernel)


__all__ = ["Bridged", "bridge"]
