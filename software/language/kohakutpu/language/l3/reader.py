"""`fn NAME.l3` bodies to L3 nodes, and back to text.

A value op is ``name = op operands attrs : type``; an operand is a name, a
constant or a view (``x[h, i, :]``, ``m[:, *]``). `module` reads every L3 body
of a `.ktpu` module and verifies it against `ops.OPS`; `fn_text` prints one
function in the form `module` reads.
"""

import math

from kohakutpu.language.l3 import nodes as N
from kohakutpu.language.l3.ops import OPS
from kohakutpu.language.l3.verify import verify
from kohakutpu.language.text.reader import Reader, dec
from kohakutpu.language.text.syntax import (
    Arrow,
    Assign,
    Call,
    Float,
    Int,
    Member,
    Name,
    Neg,
    NewAxis,
    Slice,
    Span,
    Stmt,
    Typed,
    View,
    emit,
)

SPECIAL = {"inf": float("inf"), "nan": float("nan")}


# ==================================================================== read
class L3Reader(Reader):
    def __init__(self, source: str, file: str) -> None:
        super().__init__(source, file)
        self.positions: dict = {}

    def at(self, st, node):
        self.positions[id(node)] = (st.line, st.col)
        return node

    def dim(self, st, t):
        match t:
            case Int(v, _):
                return v
            case Name(text):
                return text
        self.fail(st, f"wanted a dimension (an int or a name), got {t!r}")

    def type(self, st, t) -> N.Type:
        match t:
            case Name(text):
                return N.Type(text)
            case View(dtype, slices):
                return N.Type(dtype, tuple(self.dim(st, d) for d in slices))
        self.fail(st, f"wanted a type such as f16[M, 64], got {t!r}")

    def const(self, st, t):
        match t:
            case Int(v, _):
                return v
            case Float(v):
                return v
            case Name("true" | "false" as b):
                return b == "true"
            case Name(text) if text in SPECIAL:
                return SPECIAL[text]
            case Neg(x):
                return -self.const(st, x)
        self.fail(st, f"wanted a constant, got {t!r}")

    def operand(self, st, t):
        match t:
            case Name(text) if text in SPECIAL:
                return N.Const(SPECIAL[text])
            case Name(text):
                return N.Ref(text)
            case Int() | Float() | Neg():
                return N.Const(self.const(st, t))
            case View(name, slices):
                return N.Index(name, tuple(self.entry(st, e) for e in slices))
        self.fail(st, f"wanted an operand (a name, a constant, a view), got {t!r}")

    def entry(self, st, e):
        match e:
            case Slice(None, None):
                return N.Full()
            case Slice(lo, hi) if lo is not None and hi is not None:
                return N.Range(self.dim(st, lo), self.dim(st, hi))
            case NewAxis():
                return N.NewAxis()
            case Int() | Name():
                return N.At(self.dim(st, e))
        self.fail(st, f"wanted an index (`i`, `:`, `lo : hi`, `*`), got {e!r}")

    def domain(self, st, t):
        match t:
            case Span(lo, hi):
                return N.Span(self.dim(st, lo), self.dim(st, hi))
            case Call("tiles", (extent, size)):
                return N.Tiles(self.dim(st, extent), self.dim(st, size))
        self.fail(st, f"wanted a domain `lo..hi` or `tiles(extent, size)`, got {t!r}")


def module(ktpu) -> N.Module:
    """Every `fn NAME.l3` of a `.ktpu` module, as one verified L3 module;
    ``positions`` maps id(node) to its ``(line, col)``."""
    r = L3Reader(ktpu.source, ktpu.file)
    out = N.Module()
    for (name, level), body in ktpu.fns.items():
        if level != "l3":
            continue
        at = Stmt("fn", line=body.line, col=1)
        if body.ret is None:
            r.fail(at, f"{name}.l3 returns its result: `-> TYPE`")
        params = tuple((n, r.type(at, t)) for n, t in body.params)
        stmts = tuple(_read_stmt(r, s) for s in body.stmts)
        out.fns[name] = r.at(at, N.Fn(name, params, r.type(at, body.ret), stmts))
    out.positions = r.positions
    verify(out, OPS, ktpu.source, ktpu.file)
    return out


def _one(r, st, what):
    pos = st.positional()
    if len(pos) != 1:
        r.fail(st, f"wanted `{what}`")
    return pos[0]


def _read_stmt(r, st):
    if st.body and st.op not in ("map", "scan"):
        r.fail(st, f"`{st.op}` takes no block")
    annot = None if st.annot is None else r.type(st, st.annot)
    if st.target is not None:
        node = _read_value(r, st, annot)
    else:
        node = _read_effect(r, st)
    return r.at(st, node)


def _read_value(r, st, annot):
    name = st.target
    match st.op:
        case "output":
            if st.args or annot is None:
                r.fail(st, "wanted `NAME = output : type`")
            return N.Output(name, annot)
        case "carry":
            if annot is None:
                r.fail(st, "a carry names its type: `NAME = carry INIT : type`")
            init = r.operand(st, _one(r, st, "NAME = carry INIT : type"))
            if not isinstance(init, N.Const):
                r.fail(st, "a carry starts from a constant")
            return N.Carry(name, init, annot)
        case "inline" | "call":
            call = _one(r, st, f"NAME = {st.op} FN(args)")
            if not isinstance(call, Call):
                r.fail(st, f"wanted `NAME = {st.op} FN(args)`")
            args = tuple(r.operand(st, a) for a in call.args)
            return N.Invoke(name, call.name, args, st.op == "inline", annot)
    if st.op in ("map", "scan", "store", "next", "return", "tile"):
        r.fail(st, f"`{st.op}` binds no name")
    args = tuple(r.operand(st, a) for a in st.positional())
    attrs = tuple((k, r.const(st, v)) for k, v in st.kwargs().items())
    return N.Let(name, st.op, args, attrs, annot)


def _read_effect(r, st):
    match st.op:
        case "tile":
            kw = st.kwargs()
            if len(kw) != 1 or st.positional():
                r.fail(st, "wanted `tile NAME = INT`")
            ((name, v),) = kw.items()
            value = r.const(st, v)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                r.fail(st, "a tile is a positive int")
            return N.Tile(name, value)
        case "map" | "scan":
            vars = []
            for a in st.args:
                if not isinstance(a, Member) or not isinstance(a.lhs, Name):
                    r.fail(st, f"wanted `{st.op} VAR in DOMAIN`")
                vars.append((a.lhs.text, r.domain(st, a.rhs)))
            body = tuple(_read_stmt(r, s) for s in st.body)
            if not vars or not body:
                r.fail(st, f"`{st.op}` wants a variable and a block")
            if st.op == "map":
                return N.Map(tuple(vars), body)
            if len(vars) != 1:
                r.fail(st, "a scan has one variable")
            return N.Scan(vars[0][0], vars[0][1], body)
        case "next":
            kw = st.kwargs()
            if len(kw) != 1 or st.positional():
                r.fail(st, "wanted `next CARRY = VALUE`")
            ((name, v),) = kw.items()
            return N.Next(name, r.operand(st, v))
        case "store":
            (a,) = st.args or [None]
            if not isinstance(a, Assign) or not isinstance(a.lhs, (View, Name)):
                r.fail(
                    st, "wanted `store TENSOR[index] = VALUE` or `store TENSOR = VALUE`"
                )
            target = a.lhs if isinstance(a.lhs, View) else View(a.lhs.text, ())
            return N.Store(r.operand(st, target), r.operand(st, a.rhs))
        case "return":
            return N.Return(r.operand(st, _one(r, st, "return VALUE")))
    r.fail(st, f"`{st.op}` needs a name: `NAME = {st.op} ...`")


# =================================================================== write
def fn_text(f: N.Fn) -> str:
    """`fn NAME.l3(params) -> type` and its body, as `module` reads it."""
    params = tuple(Typed(n, _type(t)) for n, t in f.params)
    head = [Call(f"{f.name}.l3", params), Arrow("->"), _type(f.ret)]
    return emit([Stmt("fn", head, body=[_stmt(s) for s in f.body])])


def _dim(d):
    return dec(d) if isinstance(d, int) else Name(d)


def _type(t: N.Type):
    return View(t.dtype, tuple(_dim(d) for d in t.shape)) if t.shape else Name(t.dtype)


def _const(v):
    if isinstance(v, bool):
        return Name(str(v).lower())
    if isinstance(v, int):
        return dec(v)
    return Float(v) if v >= 0 or math.isnan(v) else Neg(Float(-v))


def _operand(x):
    match x:
        case N.Ref(name):
            return Name(name)
        case N.Const(v):
            return _const(v)
        case N.Index(name, entries):
            return View(name, tuple(_entry(e) for e in entries))
    raise TypeError(f"no L3 operand {x!r}")


def _entry(e):
    match e:
        case N.Full():
            return Slice()
        case N.At(i):
            return _dim(i)
        case N.Range(lo, hi):
            return Slice(_dim(lo), _dim(hi))
        case N.NewAxis():
            return NewAxis()
    raise TypeError(f"no L3 index entry {e!r}")


def _domain(d):
    match d:
        case N.Span(lo, hi):
            return Span(_dim(lo), _dim(hi))
        case N.Tiles(extent, size):
            return Call("tiles", (_dim(extent), _dim(size)))
    raise TypeError(f"no L3 domain {d!r}")


def _stmt(s) -> Stmt:
    match s:
        case N.Tile(name, value):
            return Stmt("tile", [Assign(Name(name), dec(value))])
        case N.Output(name, t):
            return Stmt("output", target=name, annot=_type(t))
        case N.Carry(name, init, t):
            return Stmt("carry", [_operand(init)], target=name, annot=_type(t))
        case N.Let(name, op, args, attrs, t):
            kws = [Assign(Name(k), _const(v)) for k, v in attrs]
            return Stmt(
                op,
                [_operand(a) for a in args] + kws,
                target=name,
                annot=None if t is None else _type(t),
                commas=True,
            )
        case N.Invoke(name, fn, args, inline, t):
            call = Call(fn, tuple(_operand(a) for a in args))
            return Stmt(
                "inline" if inline else "call",
                [call],
                target=name,
                annot=None if t is None else _type(t),
            )
        case N.Map(vars, body):
            members = [Member(Name(v), _domain(d)) for v, d in vars]
            return Stmt("map", members, body=[_stmt(b) for b in body], commas=True)
        case N.Scan(var, domain, body):
            return Stmt(
                "scan",
                [Member(Name(var), _domain(domain))],
                body=[_stmt(b) for b in body],
            )
        case N.Next(name, value):
            return Stmt("next", [Assign(Name(name), _operand(value))])
        case N.Store(target, value):
            lhs = Name(target.name) if not target.entries else _operand(target)
            return Stmt("store", [Assign(lhs, _operand(value))])
        case N.Return(value):
            return Stmt("return", [_operand(value)])
    raise TypeError(f"no L3 statement {s!r}")


__all__ = ["L3Reader", "fn_text", "module"]
