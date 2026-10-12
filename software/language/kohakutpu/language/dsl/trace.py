"""The recorder: L2 statements as a kernel body runs, and their text.

A trace holds the body's statements as a tree (a `par`, `for`, `when` or
`if sticky` statement holds its block) and prints the module: ``target``,
then ``fn NAME.l2(params)`` with the scratch buffers first. Each block's
annotations line up two columns past its longest statement, at most column
48, as the canonical printer writes them.
"""

import contextlib
from dataclasses import dataclass, field

from kohakutpu.language.dsl.errors import DSLError, at_caller
from kohakutpu.language.dsl.types import Machine

TARGET = "ktpu.v9"
ANNOT_COL = 48


@dataclass
class Stmt:
    """``[target =] op args`` with an annotation and a block."""

    op: str
    args: list = field(default_factory=list)
    target: str | None = None
    annot: str | None = None
    body: list | None = None
    #: where the author wrote it, for the L2 -> DSL source map
    where: str = ""

    def head(self) -> str:
        pos = [a for a in self.args if "=" not in a or a.startswith("(")]
        kws = [a for a in self.args if a not in pos]
        # Operands are comma-apart (`mul v, -1.5` is not `v - 1.5`); an arrow
        # or a loop header reads as words.
        words = self.op in ("par", "for", "when", "if") or any(
            a in ("->", "<-") for a in pos
        )
        args = (" " if words else ", ").join(pos)
        args = " ".join([args, *kws]) if args else " ".join(kws)
        lhs = f"{self.target} = " if self.target else ""
        return f"{lhs}{self.op}{' ' + args if args else ''}"


@dataclass
class Scope:
    """What the statements being recorded run on."""

    kind: str | None = None  # None: node scope; "mg", "vc"
    loop: bool = False  # inside the instance's stream loop (vector) / any for
    sticky: bool = False
    node: bool = False  # inside a par over nodes: one node's program


class Trace:
    """One kernel being recorded."""

    def __init__(self, name: str, machine: Machine | None = None) -> None:
        self.name = name
        self.machine = machine or Machine()
        self.params: list = []  # (name, TensorSpec)
        self.scratch: dict = {}  # name -> TensorSpec
        self.root: list = []
        self.stack: list = [self.root]
        self.scopes: list = [Scope()]
        self._ids: dict = {}
        self.taken: set = set()
        #: names bound by the author, so generated names avoid them
        self.vars: set = set()

    # ----------------------------------------------------------- recording
    @property
    def scope(self) -> Scope:
        return self.scopes[-1]

    def fresh(self, prefix: str) -> str:
        n = self._ids.get(prefix, 0)
        while True:
            n += 1
            name = f"{prefix}{n}"
            if name not in self.taken:
                break
        self._ids[prefix] = n
        self.taken.add(name)
        return name

    def claim(self, name: str) -> str:
        if name in self.taken:
            raise DSLError(f"the name {name!r} is used twice, at {at_caller()}")
        self.taken.add(name)
        return name

    def emit(self, op, *args, target=None, annot=None, kw=None) -> Stmt:
        texts = [str(a) for a in args]
        for k, v in (kw or {}).items():
            if v is not None:
                texts.append(f"{k}={v}")
        st = Stmt(op, texts, target, annot, None, at_caller())
        self.stack[-1].append(st)
        return st

    @contextlib.contextmanager
    def block(self, st: Stmt, scope: Scope | None = None):
        st.body = []
        self.stack.append(st.body)
        self.scopes.append(scope or self.scope)
        try:
            yield st
        finally:
            self.stack.pop()
            self.scopes.pop()
        if not st.body:
            raise DSLError(f"`{st.head()}` recorded an empty block, at {st.where}")

    # ------------------------------------------------------------ printing
    def text(self) -> str:
        params = ", ".join(f"{n}: {spec.text()}" for n, spec in self.params)
        lines = [f"target {TARGET}", "", f"fn {self.name}.l2({params})"]
        body = [
            Stmt("buffer", [], n, spec.text()) for n, spec in self.scratch.items()
        ] + self.root
        lines += _block(body, 1)
        return "\n".join(lines) + "\n"


def _block(stmts: list, depth: int) -> list:
    pad = "    " * depth
    heads = [pad + s.head() for s in stmts]
    width = max((len(h) for h, s in zip(heads, stmts) if s.annot), default=0)
    col = min(width + 2, ANNOT_COL)
    out = []
    for h, s in zip(heads, stmts):
        if s.annot:
            out.append(
                f"{h.ljust(col)}: {s.annot}" if len(h) < col else f"{h}  : {s.annot}"
            )
        else:
            out.append(h)
        if s.body is not None:
            out += _block(s.body, depth + 1)
    return out


#: The traces being recorded, innermost last.
_active: list = []


def active() -> Trace:
    if not _active:
        raise DSLError(
            f"kernel syntax with no kernel being traced, at {at_caller()}: call "
            "it inside a function decorated with @kt.kernel"
        )
    return _active[-1]


@contextlib.contextmanager
def recording(trace: Trace):
    _active.append(trace)
    try:
        yield trace
    finally:
        _active.pop()


__all__ = ["Scope", "Stmt", "Trace", "active", "recording"]
