"""The hand-written `.ktpu` kernels: each a kernel at L3, L2 and L1."""

import pathlib

from kohakutpu.language.text import module as _module

KERNELS = pathlib.Path(__file__).resolve().parent


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


__all__ = ["KERNELS", "kernel", "load", "names"]
