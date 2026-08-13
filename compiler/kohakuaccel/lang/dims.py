"""Symbolic extents: named axis sizes and the arithmetic over them.

An extent stays an expression until a shape supplies numbers. The operators are
addition, subtraction, multiplication, floor division and `ceildiv`.
"""

from dataclasses import dataclass


class DimError(ValueError):
    """An extent that cannot be resolved, and what is missing."""


class Dim:
    """One symbolic extent, or an expression over them."""

    def __add__(self, other):
        return Expr("+", self, other)

    def __sub__(self, other):
        return Expr("-", self, other)

    def __mul__(self, other):
        return Expr("*", self, other)

    __rmul__ = __mul__

    def __floordiv__(self, other):
        return Expr("//", self, other)

    def resolve(self, env: dict) -> int:
        raise NotImplementedError

    def free(self) -> set:
        raise NotImplementedError


@dataclass(frozen=True)
class Name(Dim):
    """A named extent, supplied at compile time."""

    name: str

    def resolve(self, env: dict) -> int:
        if self.name not in env:
            raise DimError(
                f"extent {self.name!r} was never given a value; compile with "
                f"{self.name}=..."
            )
        return int(env[self.name])

    def free(self) -> set:
        return {self.name}

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class Expr(Dim):
    """An arithmetic combination of extents and integers."""

    op: str
    lhs: object
    rhs: object

    def resolve(self, env: dict) -> int:
        a = resolve(self.lhs, env)
        b = resolve(self.rhs, env)
        match self.op:
            case "+":
                return a + b
            case "-":
                return a - b
            case "*":
                return a * b
            case "//":
                return a // b
            case "ceildiv":
                return -(-a // b)
        raise DimError(f"unknown operator {self.op!r}")

    def free(self) -> set:
        return free(self.lhs) | free(self.rhs)

    def __str__(self) -> str:
        if self.op == "ceildiv":
            return f"ceildiv({self.lhs}, {self.rhs})"
        return f"({self.lhs} {self.op} {self.rhs})"


def dims(names: str) -> tuple:
    """Named extents from a comma- or space-separated string of names."""
    parts = [n.strip() for n in names.replace(",", " ").split() if n.strip()]
    return tuple(Name(n) for n in parts)


def ceildiv(a, b) -> Dim:
    """Rounded-up division, kept symbolic."""
    return Expr("ceildiv", a, b)


def resolve(value, env: dict) -> int:
    """`value` as an integer, resolving any symbolic part against `env`."""
    if isinstance(value, Dim):
        return value.resolve(env)
    return int(value)


def free(value) -> set:
    """Every extent name `value` depends on."""
    return value.free() if isinstance(value, Dim) else set()
