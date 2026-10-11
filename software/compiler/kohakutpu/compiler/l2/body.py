"""An `fn NAME.l2` body read to a tree (docs/spec/l2-program.md §3).

    par u in 0..4 on mg[u]                  Par: instances on units
        for k in 0..16                      For: ordered iterations
            a = fill x[i*256 : +256, k*64 : +64] : mx7a[256, 64] @A
            drain acc -> y[i*256 : +256, u*256 : +256]

The reader keeps loops as loops (a vector core's loop may become a hardware
loop); `when`, `expand` and constant arithmetic are folded first. `instances`
walks the tree with every `par` and `for` variable bound.
"""

from dataclasses import dataclass, field

from kohakutpu.compiler.layout.buffer import Buffer
from kohakutpu.language.text.meta import Expander
from kohakutpu.language.text.syntax import (
    Call,
    Int,
    Member,
    Name,
    Placed,
    Span,
    Stmt,
    View,
)

#: Unit kinds a `par` may name, and the machine's class for each.
KINDS = {"node": None, "mg": "MG", "vc": "VC"}


@dataclass(frozen=True)
class Type:
    """A value's ``DTYPE[shape] @SPACE[(mode)]``."""

    dtype: str
    shape: tuple
    space: str
    mode: str | None = None

    def __str__(self) -> str:
        mode = f"({self.mode})" if self.mode else ""
        return f"{self.dtype}[{', '.join(map(str, self.shape))}] @{self.space}{mode}"


@dataclass
class Op:
    """A value (`target` set) or an effect: the op, its terms, its type."""

    st: Stmt
    op: str
    target: str | None
    args: list
    type: Type | None


@dataclass
class Par:
    st: Stmt
    var: str
    lo: object
    hi: object
    kind: str
    index: object  # a term, or None for `[any]`
    body: list


@dataclass
class For:
    st: Stmt
    var: str
    lo: object
    hi: object
    body: list
    #: at node scope, how many iterations the node may send ahead (`pipe=K`)
    pipe: int | None = None


@dataclass
class When:
    """`when COND`: the block where the condition holds for an instance."""

    st: Stmt
    cond: object
    body: list


@dataclass
class If:
    """`if sticky`: a block a unit runs or skips at run time (a vector
    core's EXP2D overflow, GETSTK + VSKIPZ)."""

    st: Stmt
    cond: object
    body: list


@dataclass
class Body:
    name: str
    params: list  # [(name, Buffer)]
    scratch: dict  # name -> Buffer
    stmts: list  # Op | Par | For
    reader: object
    expander: object = None
    buffers: dict = field(default_factory=dict)


def read(module, name: str) -> Body:
    """`fn name.l2` of `module`, as a tree; raises `TextError` at the line."""
    src = module.body(name, "l2")
    r = module.reader()
    macros = {m.name: (m.params, m.stmts) for m in module.macros.values()}
    ex = Expander(macros, r.fail)
    stmts = ex.stmts(src.stmts, {})
    params = []
    for p, t in src.params:
        try:
            params.append((p, Buffer.of(t)))
        except (TypeError, ValueError) as e:
            r.fail(stmts[0] if stmts else src, f"parameter {p}: {e}")
    scratch = {}
    while stmts and stmts[0].op == "buffer":
        st = stmts.pop(0)
        if st.target is None or st.annot is None:
            r.fail(st, "NAME = buffer : TYPE @SPACE(LAYOUT)")
        try:
            scratch[st.target] = Buffer.of(st.annot)
        except (TypeError, ValueError) as e:
            r.fail(st, str(e))
    body = Body(name, params, scratch, [], r, ex)
    body.buffers = {**dict(params), **scratch}
    body.stmts = [_node(r, st) for st in stmts]
    return body


def _node(r, st: Stmt):
    if st.op == "when" and st.target is None:
        return When(st, st.positional()[0], [_node(r, s) for s in st.body])
    if st.op == "if" and st.target is None:
        if st.positional() != [Name("sticky")] or not st.body:
            r.fail(st, "if sticky (a block): the only run-time condition")
        return If(st, Name("sticky"), [_node(r, s) for s in st.body])
    if st.op in ("par", "for") and st.target is None:
        pos = st.positional()
        if (
            not pos
            or not isinstance(pos[0], Member)
            or not isinstance(pos[0].lhs, Name)
        ):
            r.fail(st, f"wanted `{st.op} VAR in LO..HI ...`")
        dom = pos[0].rhs
        if not isinstance(dom, Span):
            r.fail(st, f"`{st.op}` ranges over LO..HI, got {dom!r}")
        var = pos[0].lhs.text
        body = [_node(r, s) for s in st.body]
        if not body:
            r.fail(st, f"`{st.op}` has no body")
        if st.op == "for":
            kw = st.kwargs()
            if len(pos) != 1 or set(kw) - {"pipe"}:
                r.fail(st, "for VAR in LO..HI [pipe=K]")
            pipe = kw.get("pipe")
            if pipe is not None and not (isinstance(pipe, Int) and pipe.value >= 1):
                r.fail(st, "pipe=K: K iterations in flight, K >= 1")
            return For(st, var, dom.lo, dom.hi, body, pipe and pipe.value)
        if len(pos) != 3 or pos[1] != Name("on") or not isinstance(pos[2], View):
            r.fail(st, "par VAR in LO..HI on UNIT[EXPR | any]")
        unit = pos[2]
        if unit.name not in KINDS or len(unit.slices) != 1:
            r.fail(st, f"a par runs on {', '.join(f'{k}[i]' for k in KINDS)}")
        index = None if unit.slices[0] == Name("any") else unit.slices[0]
        if unit.name == "node" and index is None:
            r.fail(st, "nodes are named: node[EXPR]")
        return Par(st, var, dom.lo, dom.hi, unit.name, index, body)
    if st.body:
        r.fail(st, f"`{st.op}` takes no block")
    ty = None
    if st.annot is not None:
        ty = _type(r, st, st.annot)
    return Op(st, st.op, st.target, list(st.args), ty)


def _type(r, st, t) -> Type:
    if not isinstance(t, Placed):
        r.fail(st, f"a value's type is DTYPE[shape] @SPACE, got {t!r}")
    x, space = t.x, t.space
    mode = None
    if isinstance(space, Call):
        if len(space.args) != 1 or not isinstance(space.args[0], Name):
            r.fail(st, f"@SPACE(MODE) takes one name, got {space!r}")
        mode = space.args[0].text
        space = Name(space.name)
    if not isinstance(space, Name) or not isinstance(x, View):
        r.fail(st, f"a value's type is DTYPE[shape] @SPACE, got {t!r}")
    if not all(isinstance(d, Int) for d in x.slices):
        r.fail(st, f"a value's shape is constant, got {x!r}")
    return Type(x.name, tuple(d.value for d in x.slices), space.text, mode)


# ------------------------------------------------------------------ evaluation
def value(body: Body, st, t, env: dict):
    """`t` with `env` bound and constants folded."""
    return body.expander.term(t, env, st)


def const(body: Body, st, t, env: dict) -> int:
    v = value(body, st, t, env)
    if not isinstance(v, Int):
        body.reader.fail(st, f"wanted a constant here, got {v!r}")
    return v.value


def span(body: Body, node, env: dict) -> range:
    return range(const(body, node.st, node.lo, env), const(body, node.st, node.hi, env))


@dataclass
class Instance:
    """One `par` instance: its unit (`kind`, index or None for any), the node
    it is on, the variables bound, and its items in program order: ``(Op,
    env)`` for every op, each `for` unrolled."""

    kind: str
    index: int | None
    node: int
    env: dict
    par: Par
    items: list = field(default_factory=list)
    #: item index where each iteration of the outermost `for` starts
    cuts: list = field(default_factory=list)


def instances(body: Body, stmts=None, env=None, node: int = 0, tag=None) -> list:
    """Every top-level unit instance under `stmts`, nodes resolved: a list of
    stages ``(kind, node, x, tag)``, `x` a list of instances that run
    concurrently (one `par`) or an `Op` with its env (a node-level
    statement); `tag` is ``(loop, iteration, pipe)`` inside a node-scope
    `for ... pipe=K` (the innermost), else None."""
    out = []
    for s in body.stmts if stmts is None else stmts:
        e = {} if env is None else env
        if isinstance(s, Par) and s.kind == "node":
            for v in span(body, s, e):
                inner = {**e, s.var: v}
                n = const(body, s.st, s.index, inner)
                out += instances(body, s.body, inner, n, tag)
        elif isinstance(s, Par):
            stage = []
            for v in span(body, s, e):
                inner = {**e, s.var: v}
                idx = None if s.index is None else const(body, s.st, s.index, inner)
                inst = Instance(s.kind, idx, node, inner, s)
                _flatten(body, s.body, inner, inst, top=True)
                stage.append(inst)
            out.append(("par", node, stage, tag))
        elif isinstance(s, For):
            for k, v in enumerate(span(body, s, e)):
                inner_tag = (id(s), k, s.pipe) if s.pipe else tag
                out += instances(body, s.body, {**e, s.var: v}, node, inner_tag)
        elif isinstance(s, When):
            if const(body, s.st, s.cond, e):
                out += instances(body, s.body, e, node, tag)
        elif isinstance(s, If):
            body.reader.fail(s.st, "if sticky is a vector core's (its stream loop)")
        else:
            out.append(("op", node, (s, e), tag))
    return out


def _flatten(body: Body, stmts, env: dict, inst: Instance, top: bool) -> None:
    for s in stmts:
        if isinstance(s, Par):
            body.reader.fail(s.st, "a par inside a unit's instance")
        if isinstance(s, If):
            if inst.kind != "vc":
                body.reader.fail(s.st, "if sticky is a vector core's (its stream loop)")
            inst.items.append((s, env))
            continue
        if isinstance(s, When):
            if const(body, s.st, s.cond, env):
                _flatten(body, s.body, env, inst, top)
            continue
        if isinstance(s, For):
            for v in span(body, s, env):
                if top:
                    inst.cuts.append(len(inst.items))
                _flatten(body, s.body, {**env, s.var: v}, inst, top=False)
        else:
            inst.items.append((s, env))


__all__ = [
    "KINDS",
    "Body",
    "For",
    "If",
    "Instance",
    "Op",
    "Par",
    "Type",
    "When",
    "const",
    "instances",
    "read",
    "span",
    "value",
]
