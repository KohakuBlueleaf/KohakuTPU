"""Vocabularies: what a level's statements mean.

A `Vocabulary` maps a statement keyword to a READER (``fn(reader, stmt)``) and
an IR class to a WRITER (``fn(writer, obj) -> Stmt | [Stmt]``). A level's
framework module builds one; a project registers more words on it (its unit
ops, layouts, item kinds) without touching the grammar.
"""

from kohakuaccel.text.syntax import Int, Name, Neg, Stmt, TextError, Tuple


class Vocabulary:
    def __init__(self, name: str) -> None:
        self.name = name
        self.readers: dict = {}
        self.writers: dict = {}

    def reads(self, *ops):
        """Register a reader for statements whose keyword is in `ops`."""

        def put(fn):
            for op in ops:
                self.readers[op] = fn
            return fn

        return put

    def writes(self, *types):
        """Register a writer for IR objects of the given classes."""

        def put(fn):
            for t in types:
                self.writers[t] = fn
            return fn

        return put

    def read(self, reader, st: Stmt):
        fn = self.readers.get(st.op)
        if fn is None:
            reader.fail(st, f"{self.name} has no statement {st.op!r}")
        return fn(reader, st)

    def write(self, writer, obj):
        for t in type(obj).__mro__:
            fn = self.writers.get(t)
            if fn is not None:
                out = fn(writer, obj)
                return out if isinstance(out, list) else [out]
        raise TypeError(f"{self.name} has no text for {type(obj).__name__}")


class Reader:
    """Reading state: the source for diagnostics, names bound so far."""

    def __init__(self, source: str, file: str = "<text>") -> None:
        self.source, self.file = source, file
        self.names: dict = {}

    def fail(self, st, message: str):
        raise TextError(message, st.line, st.col, self.source, self.file)

    # Common term readers ------------------------------------------------
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


def hexa(v: int) -> Int:
    return Int(v, "hex")


def dec(v: int) -> Int:
    return Int(v, "dec") if v >= 0 else Neg(Int(-v, "dec"))


def num(v: int) -> Int:
    """A byte offset: hexadecimal, zero as ``0``."""
    return hexa(v) if v > 0 else dec(v)


def size(v: int) -> Int:
    """A byte size: K/M/G when whole, else decimal."""
    return Int(v, "size") if v and v % 1024 == 0 else dec(v)


def tup(*vs) -> Tuple:
    return Tuple(tuple(dec(v) if isinstance(v, int) else v for v in vs))
