"""Symbolic integers: loop and instance variables and the index arithmetic
over them, recorded rather than evaluated.

A `Sym` is an affine sum ``const + sum(coef * atom)``; an atom is a
variable, or a floor division, modulo or product the affine form cannot
hold. Arithmetic that leaves no atom folds back to a plain int, so
``(t + 16) - t`` is ``16`` and a slice ``x[t*16 : t*16 + 16]`` prints as
``t*16 : +16``. A `Sym` refuses every question about its value (`bool`,
`int`, use as an index): the body is traced once for all of its values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn

from kohakutpu.language.dsl.errors import TracerControlFlowError, at_caller
from kohakutpu.language.dsl.mutant import ATOM_SUM_SHORTCUT, MUTANT


def _refuse(what: str, x) -> TracerControlFlowError:
    return TracerControlFlowError(
        f"{what} on the symbolic index {x.text()}, at {at_caller()}\n"
        "  A kt.range / kt.par variable is recorded once for all its values, so "
        "its value is not known while tracing. Branch with `with kt.when(...)` "
        "(a constant per instance), or loop with Python range(...) to unroll."
    )


class Sym:
    """``const + sum(coef * atom)`` over atoms (`Var` or `Atom`)."""

    __slots__ = ("const", "terms")

    def __init__(self, terms: dict, const: int = 0) -> None:
        self.terms = {a: c for a, c in terms.items() if c}
        self.const = const

    # ------------------------------------------------------------ building
    @staticmethod
    def of(x) -> Sym:
        if isinstance(x, Sym):
            return x
        if isinstance(x, bool) or not isinstance(x, int):
            raise TypeError(f"index arithmetic takes ints, got {type(x).__name__}")
        return Sym({}, x)

    def _done(self) -> Index:
        """A plain int when no atom is left."""
        return self.const if not self.terms else self

    def __add__(self, o: Index) -> Index:
        if not isinstance(o, (int, Sym)) or isinstance(o, bool):
            return NotImplemented
        o = Sym.of(o)
        terms = dict(self.terms)
        for a, c in o.terms.items():
            terms[a] = terms.get(a, 0) + c
        return Sym(terms, self.const + o.const)._done()

    def __radd__(self, o: int) -> Index:
        return self.__add__(o)

    def __neg__(self) -> Index:
        return Sym({a: -c for a, c in self.terms.items()}, -self.const)._done()

    def __sub__(self, o: Index) -> Index:
        if not isinstance(o, (int, Sym)) or isinstance(o, bool):
            return NotImplemented
        return self + (-Sym.of(o))

    def __rsub__(self, o: int) -> Index:
        return Sym.of(o) + (-self)

    def __mul__(self, o: Index) -> Index:
        if isinstance(o, bool) or not isinstance(o, (int, Sym)):
            return NotImplemented
        if isinstance(o, int):
            return Sym(
                {a: c * o for a, c in self.terms.items()}, self.const * o
            )._done()
        return Sym({Atom("*", self, o): 1})

    def __rmul__(self, o: int) -> Index:
        return self.__mul__(o)

    def __floordiv__(self, o: Index) -> Index:
        if isinstance(o, bool) or not isinstance(o, (int, Sym)):
            return NotImplemented
        if isinstance(o, int):
            if o == 1:
                return self
            if all(c % o == 0 for c in self.terms.values()) and self.const % o == 0:
                return Sym(
                    {a: c // o for a, c in self.terms.items()}, self.const // o
                )._done()
        return Sym({Atom("/", self, o): 1})

    def __rfloordiv__(self, o: int) -> Index:
        return Sym({Atom("/", Sym.of(o), self): 1})

    def __mod__(self, o: Index) -> Index:
        if isinstance(o, bool) or not isinstance(o, (int, Sym)):
            return NotImplemented
        if isinstance(o, int) and o == 1:
            return 0
        return Sym({Atom("%", self, o): 1})

    def __rmod__(self, o: int) -> Index:
        return Sym({Atom("%", Sym.of(o), self): 1})

    # ---------------------------------------------------------- comparing
    def __lt__(self, o: Index) -> Cond:
        return Cond("<", self, o)

    def __le__(self, o: Index) -> Cond:
        return Cond("<=", self, o)

    def __gt__(self, o: Index) -> Cond:
        return Cond(">", self, o)

    def __ge__(self, o: Index) -> Cond:
        return Cond(">=", self, o)

    # `==` records a condition for `kt.when`, as `<` does (`same` compares).
    def __eq__(self, o: object) -> Cond:  # type: ignore[override]
        return Cond("==", self, o)

    def __ne__(self, o: object) -> Cond:  # type: ignore[override]
        return Cond("!=", self, o)

    def __hash__(self):
        return hash((tuple(self.terms.items()), self.const))

    def same(self, o) -> bool:
        """Structural equality (``==`` builds a condition)."""
        return isinstance(o, Sym) and self.terms == o.terms and self.const == o.const

    # ------------------------------------------------------------ refusing
    # Typed NoReturn so a checker flags `if i:` on an index in the editor.
    def __bool__(self) -> NoReturn:
        raise _refuse("bool()", self)

    def __int__(self) -> NoReturn:
        raise _refuse("int()", self)

    def __index__(self) -> NoReturn:
        raise _refuse("use as an index", self)

    def __float__(self) -> NoReturn:
        raise _refuse("float()", self)

    def __iter__(self) -> NoReturn:
        raise _refuse("iteration", self)

    # ------------------------------------------------------------ printing
    def text(self) -> str:
        parts = []
        for a, c in self.terms.items():
            t = a.text()
            if c == 1:
                parts.append(("+", t))
            elif c == -1:
                parts.append(("-", t))
            elif c > 0:
                parts.append(("+", f"{t}*{c}"))
            else:
                parts.append(("-", f"{t}*{-c}"))
        if self.const > 0:
            parts.append(("+", str(self.const)))
        elif self.const < 0:
            parts.append(("-", str(-self.const)))
        # A leading minus reads poorly after `..`: a positive term goes first.
        parts.sort(key=lambda p: p[0] == "-")
        out = ""
        for i, (sign, t) in enumerate(parts):
            if i == 0:
                out = t if sign == "+" else f"-{t}"
            else:
                out += f" {sign} {t}"
        return out

    def __repr__(self) -> str:
        return f"Sym({self.text()})"

    def evaluate(self, env: dict) -> int:
        out = self.const
        for a, c in self.terms.items():
            out += c * a.evaluate(env)
        return out

    def free(self) -> set:
        out = set()
        for a in self.terms:
            out |= a.free()
        return out


#: An index: a plain int, or a `Sym` (arithmetic that cancels every atom is an int).
Index = int | Sym


@dataclass(frozen=True, eq=False)
class Var:
    """A `kt.range` / `kt.par` variable."""

    name: str

    def text(self) -> str:
        return self.name

    def evaluate(self, env: dict) -> int:
        return env[self.name]

    def free(self) -> set:
        return {self.name}

    def sym(self) -> Sym:
        return Sym({self: 1})


@dataclass(frozen=True, eq=False)
class Atom:
    """A division, modulo or product the affine form cannot hold."""

    op: str  # "/" (floor division), "%", "*"
    a: object
    b: object

    def text(self) -> str:
        return f"({_text(self.a)} {self.op} {_text(self.b)})"

    def evaluate(self, env: dict) -> int:
        a, b = evaluate(self.a, env), evaluate(self.b, env)
        return {"/": a // b if b else 0, "%": a % b if b else 0, "*": a * b}[self.op]

    def free(self) -> set:
        return free(self.a) | free(self.b)


@dataclass(frozen=True)
class Cond:
    """A comparison of index expressions, for `kt.when`."""

    op: str
    a: object
    b: object

    def __bool__(self) -> NoReturn:
        a = self.a if isinstance(self.a, Sym) else Sym.of(self.a)
        raise _refuse(f"bool() of a comparison ({self.text()})", a)

    def text(self) -> str:
        return f"{_text(self.a)} {self.op} {_text(self.b)}"

    def evaluate(self, env: dict) -> bool:
        a, b = evaluate(self.a, env), evaluate(self.b, env)
        return {
            "<": a < b,
            "<=": a <= b,
            ">": a > b,
            ">=": a >= b,
            "==": a == b,
            "!=": a != b,
        }[self.op]


def var(name: str) -> Sym:
    return Var(name).sym()


def _text(x) -> str:
    if isinstance(x, Sym):
        t = x.text()
        if len(x.terms) + bool(x.const) <= 1:
            return t
        if MUTANT.get() == ATOM_SUM_SHORTCUT and t[0] == "(":
            return t
        return f"({t})"
    return str(x)


def text(x) -> str:
    """L2 text of an int or a `Sym`."""
    return x.text() if isinstance(x, Sym) else str(int(x))


def evaluate(x, env: dict) -> int:
    return x.evaluate(env) if isinstance(x, Sym) else int(x)


def free(x) -> set:
    return x.free() if isinstance(x, Sym) else set()


def is_sym(x) -> bool:
    return isinstance(x, Sym)


__all__ = [
    "Atom",
    "Cond",
    "Index",
    "Sym",
    "Var",
    "evaluate",
    "free",
    "is_sym",
    "text",
    "var",
]
