"""Reading state shared by the level readers: the source for diagnostics and
the term readers every level uses."""

from kohakutpu.language.text.syntax import Int, Name, Neg, TextError, Tuple


class Reader:
    """Reading state: the source for diagnostics, names bound so far."""

    def __init__(self, source: str, file: str = "<text>") -> None:
        self.source, self.file = source, file
        self.names: dict = {}

    def fail(self, st, message: str):
        raise TextError(message, st.line, st.col, self.source, self.file)

    def int(self, st, t) -> int:
        match t:
            case Int(v, _):
                return v
            case Neg(Int(v, _)):
                return -v
        self.fail(st, f"wanted an integer, got {t!r}")

    def name(self, st, t) -> str:
        if isinstance(t, Name):
            return t.text
        self.fail(st, f"wanted a name, got {t!r}")

    def ints(self, st, t) -> tuple:
        if isinstance(t, Tuple):
            return tuple(self.int(st, x) for x in t.items)
        self.fail(st, f"wanted a tuple of integers, got {t!r}")


def dec(v: int) -> Int:
    """`v` as a decimal term."""
    return Int(v, "dec") if v >= 0 else Neg(Int(-v, "dec"))


__all__ = ["Reader", "dec"]
