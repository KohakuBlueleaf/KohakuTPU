"""Any DSL kernel on numpy arrays: upload, the forms its ports read (made on
the card), one run, every output read back.

    outs, run = call(dev, layernorm, {"x": x, "g": g, "b": b}, eps=1e-5)
    outs["y"], run.cycles
"""

import numpy as np

from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.storage import Storage
from kohakutpu.language import dsl as kt

CODES = {np.dtype(np.float16): "f16", np.dtype(np.float32): "f32"}
NUMPY = {"f16": np.float16, "f32": np.float32}


def code(dtype) -> str:
    """The DSL element type of a numpy element type a kernel reads."""
    got = CODES.get(np.dtype(dtype))
    if got is None:
        raise TypeError(f"a kernel reads f16 or f32 arrays, not {np.dtype(dtype)}")
    return got


def upload(dev, a: np.ndarray) -> Storage:
    """`a` in a new storage, its host bytes kept for other forms."""
    a = np.ascontiguousarray(a)
    s = Storage(dev, max(a.nbytes, 32))
    raw = a.tobytes()
    dev.write(s.addr, raw)
    s.host, s.dtype = raw, a.dtype
    return s


def call(dev, kernel: kt.Kernel, arrays: dict, kind: str = "call", **consts) -> tuple:
    """Run `kernel` on `arrays` (input port -> f16 / f32 array) with
    `consts`; ``({output port: array}, run)``. An output of another element
    type comes back as its raw bytes."""
    missing = set(kernel.inputs) - set(arrays)
    if missing:
        raise ValueError(
            f"{kernel.name} reads {kernel.inputs}; missing {sorted(missing)}"
        )
    given = {
        n: kt.TensorSpec.make(np.shape(a), code(np.asarray(a).dtype))
        for n, a in arrays.items()
    }
    compiled = dev.specialise(kernel, given, **consts)
    specs = dict(compiled.params)
    held, binds, outs = [], {}, {}
    try:
        for n in kernel.inputs:
            a = np.asarray(arrays[n])
            s = upload(dev, a)
            held.append(s)
            binds[n] = FM.for_port(dev, s, a.dtype, a.shape, specs[n])
        for n in kernel.outputs:
            s = Storage(dev, specs[n].nbytes())
            held.append(s)
            binds[n] = s.addr
            outs[n] = s
        run = dev.run(
            compiled,
            binds,
            kind=kind,
            info={"kernel": kernel.name, "compiled": compiled},
        )
        result = {}
        for n, s in outs.items():
            spec = specs[n]
            raw = dev.read(s.addr, spec.nbytes())
            dt = NUMPY.get(spec.dtype) if spec.rowmajor else None
            result[n] = np.frombuffer(raw, dt).reshape(spec.shape).copy() if dt else raw
        return result, run
    finally:
        for s in held:
            s.free()


__all__ = ["CODES", "NUMPY", "call", "code", "upload"]
