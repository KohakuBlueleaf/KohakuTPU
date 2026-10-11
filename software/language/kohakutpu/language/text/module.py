"""A unified IR module: one text, fns whose bodies are at any level.

    target ktpu.v9
    fn silu.l3(x: f16[N])           # a body at L3; `.l2`, `.l1` likewise
    image silu_stream(passes)       # an L1 definition a level reader expands
    macro unpk(i)                   # an L1 text macro (`expand unpk(0)`)

A body stays statements here: what its statements mean is its level's reader.
One name may carry a body per level; those bodies are one kernel written at
three levels, and the gates compare them.
"""

from dataclasses import dataclass, field

from kohakutpu.language.text.meta import macro_params
from kohakutpu.language.text.reader import Reader
from kohakutpu.language.text.syntax import Arrow, Call, Name, Typed, parse

LEVELS = ("l3", "l2", "l1")


@dataclass
class Body:
    """One fn body: `params` are ``(name, type term)``; `ret` a type term or
    None; `stmts` the statements as written."""

    name: str
    level: str
    params: tuple
    ret: object
    stmts: list
    line: int = 0


@dataclass
class Definition:
    """An `image` or a `macro`: parameter names and the statements."""

    kind: str
    name: str
    params: tuple
    stmts: list
    line: int = 0


@dataclass
class Module:
    source: str = ""
    file: str = "<ktpu>"
    target: str | None = None
    fns: dict = field(default_factory=dict)
    images: dict = field(default_factory=dict)
    macros: dict = field(default_factory=dict)

    def body(self, name: str, level: str) -> Body:
        got = self.fns.get((name, level))
        if got is None:
            have = sorted(f"{n}.{lv}" for n, lv in self.fns)
            raise KeyError(f"no fn {name}.{level} in {self.file}; it has {have}")
        return got

    def levels(self, name: str) -> list:
        return [lv for lv in LEVELS if (name, lv) in self.fns]

    def names(self) -> list:
        seen = []
        for n, _ in self.fns:
            if n not in seen:
                seen.append(n)
        return seen

    def reader(self) -> Reader:
        return Reader(self.source, self.file)


def read(text: str, file: str = "<ktpu>") -> Module:
    """The module of a `.ktpu` text; raises `TextError` at the line."""
    r = Reader(text, file)
    m = Module(source=text, file=file)
    for st in parse(text, file):
        pos = st.positional()
        match st.op:
            case "target":
                if (
                    m.target is not None
                    or len(pos) != 1
                    or not isinstance(pos[0], Name)
                ):
                    r.fail(st, "wanted one `target NAME`")
                m.target = pos[0].text
            case "fn":
                body = _fn(r, st, pos)
                key = (body.name, body.level)
                if key in m.fns:
                    r.fail(st, f"{body.name}.{body.level} defined twice")
                m.fns[key] = body
            case "image" | "macro":
                if not pos or not isinstance(pos[0], (Call, Name)):
                    r.fail(st, f"wanted `{st.op} NAME(params)`")
                name = pos[0].name if isinstance(pos[0], Call) else pos[0].text
                table = m.images if st.op == "image" else m.macros
                if name in m.images or name in m.macros:
                    r.fail(st, f"{name!r} defined twice")
                if not st.body:
                    r.fail(st, f"{st.op} {name} has no statement")
                params = macro_params(r.fail, st, pos[0])
                table[name] = Definition(st.op, name, params, st.body, st.line)
            case _:
                r.fail(
                    st, f"no top-level statement {st.op!r}; target, fn, image, macro"
                )
    return m


def _fn(r, st, pos) -> Body:
    if not pos or not isinstance(pos[0], Call):
        r.fail(st, "wanted `fn NAME.LEVEL(params) [-> type]`")
    head = pos[0].name
    name, _, level = head.rpartition(".")
    if level not in LEVELS or not name:
        r.fail(st, f"a fn is named NAME.LEVEL, LEVEL one of {LEVELS}; got {head!r}")
    params = []
    for a in pos[0].args:
        if not isinstance(a, Typed):
            r.fail(st, f"a parameter is `name: type`, got {a!r}")
        params.append((a.name, a.type))
    ret = None
    if len(pos) == 3 and pos[1] == Arrow("->"):
        ret = pos[2]
    elif len(pos) != 1:
        r.fail(st, "wanted `fn NAME.LEVEL(params) [-> type]`")
    if not st.body:
        r.fail(st, f"fn {head} has no body")
    return Body(name, level, tuple(params), ret, st.body, st.line)


__all__ = ["LEVELS", "Body", "Definition", "Module", "read"]
