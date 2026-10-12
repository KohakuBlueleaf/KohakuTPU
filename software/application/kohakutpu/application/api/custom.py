"""Your own DSL kernel as a function of `Tensor`s: each input handed over in
the form its port reads (f16, f32, or an MXFP7 packing made on the card and
kept while the tensor is unwritten), each output a new tensor."""

import numpy as np

from kohakutpu.application.api.tensor import Buf, Tensor, Unsupported
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.call import NUMPY
from kohakutpu.application.runtime.storage import FLAT, Storage
from kohakutpu.compiler import kernel as K
from kohakutpu.language import dsl as kt


def call(kernel: kt.Kernel, *args: Tensor, **consts) -> tuple[Tensor, ...]:
    """Run `kernel` on `args` (one an input port, in order) with `consts`;
    every output tensor, in port order."""
    ins, outs = kernel.inputs, kernel.outputs
    if len(args) != len(ins):
        raise ValueError(f"{kernel.name} reads {ins}, given {len(args)} tensors")
    dev = D.device()
    for t in args:
        if t._T is not None or t._stat:
            raise Unsupported(
                f"{kernel.name}: a transposed view or a row statistic as a kernel input"
            )
        t.realize()
    given = {
        n: kt.TensorSpec.make(t.shape, "f32" if t.dtype == np.float32 else "f16")
        for n, t in zip(ins, args)
    }
    compiled = K.specialise(kernel, given, machine=dev.machine, **consts)
    specs = dict(compiled.params)
    binds = {
        n: FM.for_port(dev, t.st, t.dtype, t.shape, specs[n]) for n, t in zip(ins, args)
    }
    results = []
    for n in outs:
        spec = specs[n]
        if not spec.rowmajor or spec.dtype not in NUMPY:
            raise Unsupported(
                f"{kernel.name}: output {n} is {spec.text()}; a Tensor holds row-major f16 / f32"
            )
        st = Storage(dev, spec.nbytes())
        binds[n] = st.addr
        results.append((n, st, spec))
    dev.run(
        compiled,
        binds,
        kind="custom",
        info={"kernel": kernel.name, "compiled": compiled},
    )
    out = []
    for _, st, spec in results:
        st.wrote(FLAT)
        st.dtype = np.dtype(NUMPY[spec.dtype])
        out.append(Tensor(spec.shape, Buf(st, st.dtype)))
    return tuple(out)


class Function:
    """`kernel` as a function of tensors: calling it returns the output of a
    one-output kernel; `outputs` returns every output, in port order."""

    def __init__(self, kernel: kt.Kernel) -> None:
        self.kernel = kernel
        self.__name__ = kernel.name
        self.__doc__ = (
            f"`{kernel.name}` on tensors: "
            f"{', '.join(kernel.inputs)} -> {', '.join(kernel.outputs)}."
        )

    def __call__(self, *args: Tensor, **consts) -> Tensor:
        if len(self.kernel.outputs) != 1:
            raise ValueError(
                f"{self.kernel.name} writes {self.kernel.outputs}: call .outputs(...)"
            )
        return call(self.kernel, *args, **consts)[0]

    def outputs(self, *args: Tensor, **consts) -> tuple[Tensor, ...]:
        return call(self.kernel, *args, **consts)


def function(kernel: kt.Kernel) -> Function:
    """`kernel` as a function of tensors: ``silu = function(my_silu)``."""
    return Function(kernel)


__all__ = ["Function", "call", "function"]
