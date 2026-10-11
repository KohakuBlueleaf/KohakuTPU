"""Hand-written examples: standalone `.ktpu` files under `l1/` (each one `fn
NAME.l1`, with the images and macros it uses, exercising hardware features and
kernels at the level the node runs) and under `l2/` (each one `fn NAME.l2`, the
same kernel a level up, compiled to L1)."""

import pathlib

from kohakutpu.language.text import module as _module

EXAMPLES = pathlib.Path(__file__).resolve().parent


def load(path) -> _module.Module:
    p = pathlib.Path(path)
    return _module.read(p.read_text(encoding="utf-8"), str(p))


def example(name: str, level: str = "l1") -> pathlib.Path:
    """The example file `name` at `level` (`l1/mover/gather.ktpu` is
    `gather`); a name ending ``.ktpu`` is a file path, read as it is."""
    if name.endswith(".ktpu"):
        return pathlib.Path(name)
    hits = sorted((EXAMPLES / level).rglob(f"{name}.ktpu"))
    if len(hits) != 1:
        raise FileNotFoundError(f"{name}.ktpu under {EXAMPLES / level}: {hits}")
    return hits[0]


def names(level: str = "l1") -> list:
    return sorted(p.stem for p in (EXAMPLES / level).rglob("*.ktpu"))


__all__ = ["EXAMPLES", "example", "load", "names"]
