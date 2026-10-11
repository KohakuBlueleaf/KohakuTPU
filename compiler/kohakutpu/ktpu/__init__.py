"""`.ktpu`: KohakuTPU's one IR language, a body at L3, L2 or L1 per `fn`.

`kohakuaccel.text` parses a file into a module (`module.read`); the level
readers here give each body its meaning: `l1.vector` encodes a V2 vector-core
`image`, `l1.node` turns an `fn NAME.l1` body into an L1 `Program`,
`l3.reference` runs an `fn NAME.l3` body in numpy. Hand-written kernels are
in `kernels/`.
"""

import pathlib

from kohakuaccel.text import module as _module

KERNELS = pathlib.Path(__file__).resolve().parent / "kernels"


def load(path) -> _module.Module:
    """The module of a `.ktpu` file."""
    p = pathlib.Path(path)
    return _module.read(p.read_text(encoding="utf-8"), str(p))


def kernel(name: str) -> pathlib.Path:
    """The hand-written kernel file `name` (`silu`, `softmax`, ...)."""
    hits = sorted(KERNELS.rglob(f"{name}.ktpu"))
    if len(hits) != 1:
        raise FileNotFoundError(f"{name}.ktpu under {KERNELS}: {hits}")
    return hits[0]


__all__ = ["KERNELS", "kernel", "load"]
