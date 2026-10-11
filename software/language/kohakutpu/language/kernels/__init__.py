"""The hand-written `.ktpu` kernels: each a kernel at L3, L2 and L1, and the
inputs a kernel is exercised with."""

import math
import pathlib

from kohakutpu.language.text import module as _module

KERNELS = pathlib.Path(__file__).resolve().parent
#: Inputs that are not activations. A weight is N(0, 1 / fan-in), its last
#: axis (LeCun); a query arrives pre-scaled by log2(e) / sqrt(d), as the
#: attention kernels state. Everything else is an activation, N(0, 1).
ROLES = {
    "mlp": {"w1": "weight", "w2": "weight"},
    "swiglu": {"wg": "weight", "wu": "weight", "wd": "weight"},
    "attention": {"q": "query"},
    "flash": {"q": "query"},
}


def scale(name: str, param: str, shape: tuple) -> float:
    """The standard deviation of kernel `name`'s input `param` of `shape`."""
    role = ROLES.get(name, {}).get(param)
    if role == "weight":
        return 1.0 / math.sqrt(shape[-1])
    if role == "query":
        return math.log2(math.e) / math.sqrt(shape[-1])
    return 1.0


def inputs(name: str, shapes: list, rng) -> list:
    """Seeded float64 inputs for kernel `name`, one per ``(param, shape)``."""
    return [rng.standard_normal(s) * scale(name, p, s) for p, s in shapes]


def load(path) -> _module.Module:
    """The module of a `.ktpu` file."""
    p = pathlib.Path(path)
    return _module.read(p.read_text(encoding="utf-8"), str(p))


def kernel(name: str) -> pathlib.Path:
    """The kernel file `name` (`silu`, `softmax`, ...)."""
    hits = sorted(KERNELS.rglob(f"{name}.ktpu"))
    if len(hits) != 1:
        raise FileNotFoundError(f"{name}.ktpu under {KERNELS}: {hits}")
    return hits[0]


def names() -> list:
    """Every kernel file's name."""
    return sorted(p.stem for p in KERNELS.rglob("*.ktpu"))


__all__ = ["KERNELS", "ROLES", "inputs", "kernel", "load", "names", "scale"]
