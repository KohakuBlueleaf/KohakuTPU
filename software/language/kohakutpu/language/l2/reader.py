"""An `fn NAME.l2` body to L2 nodes.

s = buffer                                  : f16[1024, 1024] @dram
par u in 0..4 on mg[u]                      # instances, each placed
    for t in 0..32 pipe=2                   # ordered, two in flight
        xt = load x[u*256 + t*8 : +8, :]    : f16[8, 128] @l1
        acc = gemm a[u*256 : +256, :], b k=64   : f32[256, 64] @acc
        store y[u*256 + t*8 : +8, :] <- mul xt, r[:, *]
"""

from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.text.syntax import (
    Arrow,
    Call,
    Float,
    Int,
    Member,
    Name,
    Neg,
    NewAxis,
    Placed,
    Pos,
    Slice,
    Span,
    View,
)


def body(module, name: str) -> L.Body:
    """`name.l2` of a `.ktpu` module, read; `verify.check` gives it meaning."""
    b = module.body(name, "l2")
    r = module.reader()
    out = L.Body(name, (), (), source=module.source, file=module.file)
    rd = _Reader(r, out)
    out.params = tuple((p, rd.type(None, t)) for p, t in b.params)
    out.stmts = tuple(rd.stmt(s) for s in b.stmts)
    return out


class _Reader:
    def __init__(self, r, out: L.Body) -> None:
        self.r = r
        self.out = out

    def fail(self, st, message):
        self.r.fail(st, message)

    def at(self, st, node):
        self.out.at[id(node)] = st
        return node

    # ------------------------------------------------------------- types
    def type(self, st, t) -> L.Type:
        space, layout = None, ()
        if isinstance(t, Placed):
            t, at = t.x, t.space
            if isinstance(at, Call):
                layout = tuple(
                    (a.name, self._layout_value(st, a.value)) for a in at.args
                )
                at = Name(at.name)
            if not isinstance(at, Name):
                self.fail(st, f"a space is @NAME or @NAME(key=value), got {at!r}")
            space = at.text
        if isinstance(t, Name):
            return L.Type(t.text, (), space, layout)
        if not isinstance(t, View) or not all(isinstance(d, Int) for d in t.slices):
            self.fail(st, f"an L2 type is DTYPE[constant dims] @SPACE, got {t!r}")
        return L.Type(t.name, tuple(d.value for d in t.slices), space, layout)

    def _layout_value(self, st, v):
        if isinstance(v, Int):
            return v.value
        if hasattr(v, "values"):
            return tuple(v.values)
        self.fail(st, f"a layout value is an int or dims, got {v!r}")

    # ---------------------------------------------------------- operands
    def operand(self, st, t):
        match t:
            case Int(v, _):
                return L.Const(float(v))
            case Float(v):
                return L.Const(v)
            case Neg(Int(v, _)) | Neg(Float(v)):
                return L.Const(-float(v))
            case Name(text):
                return L.View(text)
            case View(name, slices):
                return L.View(name, tuple(self.axis(st, s) for s in slices))
        self.fail(st, f"wanted a value, a view or a constant, got {t!r}")

    def axis(self, st, s) -> L.Axis:
        match s:
            case Slice(None, None):
                return L.Axis()
            case Slice(lo, Pos(Int(n, _))) if lo is not None:
                return L.Axis(lo, n)
            case Slice(Int(lo, _), Int(hi, _)):
                return L.Axis(Int(lo), hi - lo)
            case NewAxis():
                return L.Axis(new=True)
            case Slice():
                self.fail(st, f"an axis is `:`, `lo : +size`, `lo`, or `*`; got {s!r}")
        return L.Axis(s, None)

    def view(self, st, t) -> L.View:
        v = self.operand(st, t)
        if not isinstance(v, L.View):
            self.fail(st, f"wanted a view, got {t!r}")
        return v

    # -------------------------------------------------------- statements
    def stmt(self, st):
        annot = None if st.annot is None else self.type(st, st.annot)
        if st.body and st.op not in ("par", "for"):
            self.fail(st, f"`{st.op}` takes no block")
        match st.op:
            case "par":
                return self.at(st, self.par(st))
            case "for":
                return self.at(st, self.loop(st))
            case "store":
                return self.at(st, self.store(st))
        if st.target is None:
            self.fail(st, f"`{st.op}` binds a name: `NAME = {st.op} ...`")
        if annot is None:
            self.fail(st, f"`{st.target} = {st.op} ...` names its type: `: TYPE`")
        if st.op == "buffer":
            if st.args or annot.space != "dram":
                self.fail(st, "`NAME = buffer : TYPE @dram`")
            return self.at(st, L.Buffer(st.target, annot))
        if st.op == "load":
            (src,) = st.positional() or [None]
            if src is None or len(st.positional()) != 1:
                self.fail(st, "`NAME = load VIEW : TYPE @l1`")
            return self.at(st, L.Load(st.target, self.view(st, src), annot))
        return self.at(
            st, self.op(st, st.target, st.op, st.positional(), st.kwargs(), annot)
        )

    def op(self, st, name, op, pos, kw, annot) -> L.Op:
        args = tuple(self.operand(st, a) for a in pos)
        attrs = []
        for k, v in kw.items():
            if not isinstance(v, (Int, Float)):
                self.fail(st, f"an attribute is a constant, got {k}={v!r}")
            attrs.append((k, v.value))
        return L.Op(name, op, args, tuple(attrs), annot)

    def par(self, st) -> L.Par:
        pos = st.positional()
        if (
            len(pos) != 3
            or not isinstance(pos[0], Member)
            or pos[1] != Name("on")
            or not isinstance(pos[2], View)
            or len(pos[2].slices) != 1
        ):
            self.fail(st, "`par VAR in LO..HI on UNIT[EXPR]`")
        var, lo, hi = self.span(st, pos[0])
        body = tuple(self.stmt(s) for s in st.body)
        return L.Par(var, lo, hi, pos[2].name, pos[2].slices[0], body)

    def loop(self, st) -> L.For:
        pos, kw = st.positional(), st.kwargs()
        if len(pos) != 1 or not isinstance(pos[0], Member) or set(kw) - {"pipe"}:
            self.fail(st, "`for VAR in LO..HI [pipe=K]`")
        var, lo, hi = self.span(st, pos[0])
        pipe = kw.get("pipe", Int(1))
        if not isinstance(pipe, Int) or pipe.value < 1:
            self.fail(st, f"pipe= is a positive int, got {pipe!r}")
        body = tuple(self.stmt(s) for s in st.body)
        return L.For(var, lo, hi, pipe.value, body)

    def span(self, st, m) -> tuple:
        if not isinstance(m.lhs, Name) or not isinstance(m.rhs, Span):
            self.fail(st, "wanted `VAR in LO..HI`")
        lo, hi = m.rhs.lo, m.rhs.hi
        if not (isinstance(lo, Int) and isinstance(hi, Int)) or hi.value <= lo.value:
            self.fail(st, f"an L2 range is constant and not empty, got {lo!r}..{hi!r}")
        return m.lhs.text, lo.value, hi.value

    def store(self, st) -> L.Store:
        if st.target is not None:
            self.fail(st, "`store VIEW <- VALUE` binds no name")
        pos = st.positional()
        if len(pos) < 3 or pos[1] != Arrow("<-"):
            self.fail(st, "`store VIEW <- VALUE` or `store VIEW <- OP ARGS`")
        dst = self.view(st, pos[0])
        rest = pos[2:]
        if len(rest) == 1 and not st.kwargs():
            return L.Store(dst, self.operand(st, rest[0]))
        if not isinstance(rest[0], Name):
            self.fail(st, f"wanted `store VIEW <- OP ARGS`, got {rest[0]!r}")
        return L.Store(
            dst, self.op(st, None, rest[0].text, rest[1:], st.kwargs(), None)
        )


__all__ = ["body"]
