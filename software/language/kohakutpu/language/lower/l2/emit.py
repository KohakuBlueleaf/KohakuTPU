"""Shared pieces of the L2 -> L1 lowerings: constants to K / S registers,
affine index steps, the op table."""

from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.l2.verify import value

#: L2 op -> (V2 mnemonic, number of sources).
MATH = {
    "add": ("vadd", 2),
    "sub": ("vsub", 2),
    "mul": ("vmul", 2),
    "max": ("vmax", 2),
    "min": ("vmin", 2),
    "fma": ("vfma", 3),
    "exp2": ("vexp2", 1),
    "log2": ("vlog2", 1),
    "inv": ("vinv", 1),
    "rsqrt": ("vrsqrt", 1),
    "neg": ("vneg", 1),
    "abs": ("vabs", 1),
    "copy": ("vmov", 1),
}
KCONST = {0.0: "k0", 1.0: "k1", -1.0: "k2"}
#: The S register VLOOP counts with; constants take the others.
LOOP_S = 5
L1_WORDS = 512


class LowerError(ValueError):
    pass


def affine(t, env: dict, var: str) -> tuple:
    """(value at var = 0, step per var) of an index expression under `env`."""
    a, b, c = (value(t, {**env, var: i}) for i in (0, 1, 2))
    if c - b != b - a:
        raise LowerError(f"index not affine in {var}")
    return a, b - a


class Consts:
    """Constants an image reads: K registers, else S registers set once."""

    def __init__(self) -> None:
        self.s: dict = {}

    def name(self, v: float) -> str:
        if v in KCONST:
            return KCONST[v]
        if v not in self.s:
            n = len(self.s)
            n += n >= LOOP_S
            if n >= 16:
                raise LowerError("more than 15 distinct constants")
            self.s[v] = f"s{n}"
        return self.s[v]

    def emit(self, out) -> None:
        for v, s in self.s.items():
            out(f"seti {s} = {v!r}")


def uses(ops) -> dict:
    """Value name -> the indices of the ops reading it."""
    got: dict = {}
    for k, o in enumerate(ops):
        for a in o.args:
            if isinstance(a, L.View):
                got.setdefault(a.name, []).append(k)
    return got


__all__ = [
    "KCONST",
    "L1_WORDS",
    "LOOP_S",
    "MATH",
    "Consts",
    "LowerError",
    "affine",
    "uses",
]
