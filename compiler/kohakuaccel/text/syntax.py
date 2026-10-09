"""The statement syntax every IR level shares (docs/spec/ir-text.md): parse text
to `Stmt` trees, print `Stmt` trees back as canonical text.

A statement is ``[target =] op args [: annotation]`` on one line, with an
optional indented block. This module knows no keyword: a level's vocabulary
(`kohakuaccel.text.vocab`) gives statements their meaning.
"""

import math
import pathlib
from dataclasses import dataclass, field
from functools import cache

from lark import Lark, Token, Transformer, v_args
from lark.exceptions import UnexpectedCharacters, UnexpectedEOF, UnexpectedInput
from lark.indenter import DedentError, Indenter

GRAMMAR = pathlib.Path(__file__).with_name("kat.lark")


class TextError(ValueError):
    """A parse or meaning error at a line and column of a source text."""

    def __init__(
        self,
        message: str,
        line: int = 0,
        col: int = 0,
        source: str = "",
        name: str = "<text>",
    ) -> None:
        self.message, self.line, self.col, self.name = message, line, col, name
        where = f"{name}:{line}:{col}: " if line else f"{name}: "
        snippet = ""
        if line and source:
            lines = source.splitlines()
            if 0 < line <= len(lines):
                snippet = f"\n    {lines[line - 1]}\n    {' ' * max(col - 1, 0)}^"
        super().__init__(f"{where}{message}{snippet}")


# ----------------------------------------------------------------------- terms
@dataclass(frozen=True)
class Name:
    text: str


@dataclass(frozen=True)
class Int:
    """An integer and how it was written: ``dec``, ``hex`` or ``size`` (K/M/G)."""

    value: int
    style: str = "dec"


@dataclass(frozen=True)
class Float:
    value: float


@dataclass(frozen=True)
class Dims:
    values: tuple


@dataclass(frozen=True)
class Str:
    text: str


@dataclass(frozen=True)
class Neg:
    x: object


@dataclass(frozen=True)
class Not:
    x: object


@dataclass(frozen=True)
class Offset:
    base: object
    off: object


@dataclass(frozen=True)
class Span:
    lo: object
    hi: object


@dataclass(frozen=True)
class Typed:
    name: str
    type: object


@dataclass(frozen=True)
class Kw:
    name: str
    value: object


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple = ()


@dataclass(frozen=True)
class Slice:
    lo: object = None
    hi: object = None


@dataclass(frozen=True)
class NewAxis:
    pass


@dataclass(frozen=True)
class View:
    name: str
    slices: tuple = ()


@dataclass(frozen=True)
class Tuple:
    items: tuple


@dataclass(frozen=True)
class Assign:
    lhs: object
    rhs: object


@dataclass(frozen=True)
class Member:
    lhs: object
    rhs: object


@dataclass(frozen=True)
class Arrow:
    text: str


@dataclass
class Stmt:
    """One statement. `commas` is how its positional arguments print."""

    op: str
    args: list = field(default_factory=list)
    target: str | None = None
    annot: object = None
    body: list = field(default_factory=list)
    line: int = 0
    col: int = 0
    commas: bool = False

    # The vocabulary's view of the arguments.
    def positional(self) -> list:
        return [
            a
            for a in self.args
            if not (isinstance(a, Assign) and isinstance(a.lhs, Name))
        ]

    def kwargs(self) -> dict:
        return {
            a.lhs.text: a.rhs
            for a in self.args
            if isinstance(a, Assign) and isinstance(a.lhs, Name)
        }


# --------------------------------------------------------------------- parsing
class _Indenter(Indenter):
    NL_type = "_NL"
    OPEN_PAREN_types = ("LPAR", "LSQB")
    CLOSE_PAREN_types = ("RPAR", "RSQB")
    INDENT_type = "_INDENT"
    DEDENT_type = "_DEDENT"
    tab_len = 4

    def handle_NL(self, token):
        try:
            yield from super().handle_NL(token)
        except DedentError:
            col = len(str(token).rsplit("\n", 1)[-1]) + 1
            raise _BadDedent(token.end_line, col) from None


class _BadDedent(Exception):
    def __init__(self, line: int, col: int) -> None:
        self.line, self.col = line, col


def _int(tok: Token) -> Int:
    text = str(tok).replace("_", "")
    if tok.type == "HEX":
        return Int(int(text, 16), "hex")
    if tok.type == "SIZE":
        return Int(int(text[:-1]) << {"K": 10, "M": 20, "G": 30}[text[-1]], "size")
    return Int(int(text), "dec")


@v_args(inline=True)
class _Build(Transformer):
    def name(self, t):
        return Name(str(t))

    def int(self, t):
        return _int(t)

    hex = size = int

    def float(self, t):
        return Float(float(str(t).replace("_", "")))

    def dims(self, t):
        return Dims(tuple(int(v) for v in str(t).split("x")))

    def string(self, t):
        return Str(str(t)[1:-1])

    def neg(self, x):
        return Neg(x)

    def notpred(self, x):
        return Not(x)

    def offset(self, base, off):
        return Offset(base, off)

    def span(self, lo, hi):
        return Span(lo, hi)

    def typed(self, n, t):
        return Typed(str(n), t)

    def kwarg(self, n, v):
        return Kw(str(n), v)

    def cargs(self, *xs):
        return tuple(xs)

    def call(self, n, args=()):
        return Call(str(n), tuple(args))

    def slices(self, *xs):
        return tuple(xs)

    def slice_both(self, lo, hi):
        return Slice(lo, hi)

    def slice_from(self, lo):
        return Slice(lo, None)

    def slice_to(self, hi):
        return Slice(None, hi)

    def slice_all(self):
        return Slice()

    def newaxis(self):
        return NewAxis()

    def view(self, n, slices=()):
        return View(str(n), tuple(slices))

    def tuple(self, *xs):
        return Tuple(tuple(xs))

    def assign(self, a, b):
        return Assign(a, b)

    def member(self, a, b):
        return Member(a, b)

    def arrow(self, t):
        return Arrow(str(t))

    def args(self, *xs):
        commas = any(isinstance(x, Token) and x.type == "COMMA" for x in xs)
        return ("args", [x for x in xs if not isinstance(x, Token)], commas)

    def annot(self, t):
        return ("annot", t)


@cache
def _parser() -> Lark:
    return Lark(
        GRAMMAR.read_text(encoding="utf-8"),
        parser="lalr",
        lexer="contextual",
        postlex=_Indenter(),
        propagate_positions=True,
        maybe_placeholders=False,
    )


def _stmts(tree, build) -> list:
    out = []
    for st in tree.children:
        line = st.children[0]
        body = st.children[1].children if len(st.children) > 1 else []
        target = None
        if line.data == "binding":
            target, cmd = str(line.children[0]), line.children[1]
        else:
            cmd = line
        op = str(cmd.children[0])
        args, annot, commas = [], None, False
        for part in cmd.children[1:]:
            done = build.transform(part)
            if done[0] == "annot":
                annot = done[1]
            else:
                _, args, commas = done
        out.append(
            Stmt(
                op,
                args,
                target,
                annot,
                _stmts_of(body, build),
                line.meta.line,
                line.meta.column,
                commas,
            )
        )
    return out


def _stmts_of(stmts, build) -> list:
    return _stmts(_Wrap(stmts), build)


class _Wrap:
    def __init__(self, children) -> None:
        self.children = children


def parse(text: str, name: str = "<text>") -> list:
    """`Stmt` trees for `text`; raises `TextError` with the position."""
    source = text if text.endswith("\n") else text + "\n"
    try:
        tree = _parser().parse(source)
    except _BadDedent as e:
        raise TextError(
            "a dedent to no enclosing block's column", e.line, e.col, source, name
        ) from None
    except UnexpectedCharacters as e:
        raise TextError(
            f"unexpected character {source[e.pos_in_stream]!r}",
            e.line,
            e.column,
            source,
            name,
        ) from None
    except UnexpectedEOF as e:
        raise TextError(
            f"text ends where {sorted(e.expected)} was expected", 0, 0, source, name
        ) from None
    except UnexpectedInput as e:
        tok = getattr(e, "token", None)
        what = f"unexpected {tok!r}" if tok is not None else "unexpected input"
        raise TextError(what, e.line, e.column, source, name) from None
    return _stmts(tree, _Build())


# -------------------------------------------------------------------- printing
def fmt(t) -> str:
    """One term, canonically."""
    match t:
        case Name(text):
            return text
        case Int(value, "hex"):
            digits = f"{value:x}"
            groups = []
            while digits:
                groups.insert(0, digits[-4:])
                digits = digits[:-4]
            return "0x" + "_".join(groups or ["0"])
        case Int(value, "size"):
            for unit, shift in (("G", 30), ("M", 20), ("K", 10)):
                if value and value % (1 << shift) == 0:
                    return f"{value >> shift}{unit}"
            return str(value)
        case Int(value, _):
            return str(value)
        case Float(value):
            if math.isnan(value):
                return "nan"
            if math.isinf(value):
                return "inf" if value > 0 else "-inf"
            return repr(float(value))
        case Dims(values):
            return "x".join(map(str, values))
        case Str(text):
            return f'"{text}"'
        case Neg(x):
            return f"-{fmt(x)}"
        case Not(x):
            return f"!{fmt(x)}"
        case Offset(b, o):
            return f"{fmt(b)}+{fmt(o)}"
        case Span(lo, hi):
            return f"{fmt(lo)}..{fmt(hi)}"
        case Typed(n, ty):
            return f"{n}: {fmt(ty)}"
        case Kw(n, v):
            return f"{n}={fmt(v)}"
        case Call(n, args):
            return f"{n}({', '.join(fmt(a) for a in args)})"
        case Slice(lo, hi):
            return f"{fmt(lo) + ' ' if lo is not None else ''}:{' ' + fmt(hi) if hi is not None else ''}"
        case NewAxis():
            return "*"
        case View(n, slices):
            return f"{n}[{', '.join(fmt(s) for s in slices)}]"
        case Tuple(items):
            if len(items) == 1:
                return f"({fmt(items[0])},)"
            return f"({', '.join(fmt(i) for i in items)})"
        case Assign(a, b):
            return (
                f"{fmt(a)}={fmt(b)}" if isinstance(a, Name) else f"{fmt(a)} = {fmt(b)}"
            )
        case Member(a, b):
            return f"{fmt(a)} in {fmt(b)}"
        case Arrow(text):
            return text
        case int() | float():
            return fmt(Int(t) if isinstance(t, int) else Float(t))
        case str():
            return t
    raise TypeError(f"no text for {t!r}")


def _head(st: Stmt) -> str:
    if len(st.args) == 1 and isinstance(st.args[0], Assign):
        # A statement that is one binding (`tile bq = 32`, `next m = mn`).
        a = st.args[0]
        return f"{_op(st)} {fmt(a.lhs)} = {fmt(a.rhs)}"
    pos = [
        a for a in st.args if not (isinstance(a, Assign) and isinstance(a.lhs, Name))
    ]
    kws = [a for a in st.args if a not in pos]
    parts = []
    if pos:
        if st.commas:
            out, prev = [], None
            for a in pos:
                if prev is not None:
                    out.append(
                        " " if isinstance(a, Arrow) or isinstance(prev, Arrow) else ", "
                    )
                out.append(fmt(a))
                prev = a
            parts.append("".join(out))
        else:
            parts.append(" ".join(fmt(a) for a in pos))
    parts += [fmt(a) for a in kws]
    return " ".join([_op(st), *parts])


def _op(st: Stmt) -> str:
    return f"{st.target} = {st.op}" if st.target is not None else st.op


def emit(stmts: list, indent: int = 0, align: int = 48) -> str:
    """Canonical text for `stmts`. Annotations in one block line up, at least
    two spaces after the longest statement and no further than `align`; at the
    top level a statement with a block is set apart by blank lines."""
    pad = " " * (4 * indent)
    heads = [_head(s) for s in stmts]
    col = max(
        (len(h) for h, s in zip(heads, stmts, strict=True) if s.annot is not None),
        default=0,
    )
    col = min(col + 2, align)
    lines = []
    prev = None
    for h, s in zip(heads, stmts, strict=True):
        if indent == 0 and prev is not None and (s.body or prev.body):
            lines.append("")
        prev = s
        line = pad + h
        if s.annot is not None:
            line = (
                f"{line:<{len(pad) + col}}: {fmt(s.annot)}"
                if len(h) < col
                else (f"{line}  : {fmt(s.annot)}")
            )
        lines.append(line)
        if s.body:
            lines.append(emit(s.body, indent + 1, align).rstrip("\n"))
    return "\n".join(lines) + "\n"
