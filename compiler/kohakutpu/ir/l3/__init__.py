"""KohakuTPU's L3 (docs/projects/kohakutpu/ir/l3.md): its ops (`ops.OPS`) on the
framework's L3 (`kohakuaccel.ir.l3`), and the reference kernels written in it
(``kernels/*.l3``)."""

import pathlib

from kohakuaccel.ir.l3 import interp, verify
from kohakuaccel.ir.l3 import text as _text
from kohakutpu.ir.l3.ops import OPS

KERNELS = pathlib.Path(__file__).with_name("kernels")


def read(text: str, file: str = "<l3>"):
    """A verified module of an L3 text."""
    module = _text.read(text, file)
    verify.verify(module, OPS, text, file)
    return module


def kernel(name: str):
    """The reference kernel module ``kernels/NAME.l3``."""
    path = KERNELS / f"{name}.l3"
    return read(path.read_text(encoding="utf-8"), str(path))


def write(module) -> str:
    return _text.write(module)


def run(module, program: str, inputs: dict) -> dict:
    """`program`'s outputs on `inputs`, in the numpy reference."""
    return interp.run(module, OPS, program, inputs)


__all__ = ["KERNELS", "OPS", "kernel", "read", "run", "write"]
