"""The L3 verifier: names bound once before use, every op the project's with
its arity, attributes and dtype, every shape inferred and equal to the one the
statement names, carries consumed by one scan and updated once by it, stores
into outputs only, functions acyclic and returning their type.

Dims are ints or symbols; a tile name stands for its int, so two dims agree
when they are the same int or the same symbol.
"""

from dataclasses import dataclass

from kohakuaccel.ir.l3 import nodes as N
from kohakuaccel.ir.l3.ops import ShapeError, fmt_shape
from kohakuaccel.text.syntax import TextError


class VerifyError(ValueError):
    def __init__(self, message: str, node=None) -> None:
        super().__init__(message)
        self.message, self.node = message, node


@dataclass(frozen=True)
class Entry:
    """What a name is: ``tensor`` (a parameter), ``output``, ``value``,
    ``carry``, ``point``/``tile`` (a loop variable; `size` a tile's) or
    ``dim`` (a tile constant or a shape symbol; `size` its int or itself)."""

    kind: str
    type: N.Type | None = None
    size: object = None
    extent: object = None


class Scope:
    def __init__(self, parent=None) -> None:
        self.parent, self.names = parent, {}

    def get(self, name):
        s = self
        while s is not None:
            if name in s.names:
                return s.names[name]
            s = s.parent
        return None

    def bind(self, name, entry, node) -> None:
        if self.get(name) is not None:
            raise VerifyError(f"{name!r} is already bound", node)
        self.names[name] = entry


class _Verifier:
    def __init__(self, module: N.Module, ops) -> None:
        self.module, self.ops = module, ops
        self.fn_types: dict = {}
        self.visiting: list = []

    # -------------------------------------------------------------- types
    def dim(self, scope, d, node):
        if isinstance(d, int):
            return d
        e = scope.get(d)
        if e is None or e.kind != "dim":
            raise VerifyError(f"no dimension {d!r}", node)
        return e.size

    def type(self, scope, t: N.Type, node) -> N.Type:
        if t.dtype not in self.ops.dtypes:
            raise VerifyError(f"no dtype {t.dtype!r}", node)
        return N.Type(t.dtype, tuple(self.dim(scope, d, node) for d in t.shape))

    def bind_symbols(self, scope, params, node) -> None:
        for _, t in params:
            for d in t.shape:
                if isinstance(d, str) and scope.get(d) is None:
                    scope.bind(d, Entry("dim", size=d), node)

    # ----------------------------------------------------------- operands
    def operand(self, scope, x, node) -> N.Type:
        match x:
            case N.Const():
                return N.Type("const")
            case N.Ref(name):
                e = scope.get(name)
                if e is None:
                    raise VerifyError(f"{name!r} is not bound", node)
                if e.kind == "dim":
                    return N.Type("const")
                if e.kind in ("point", "tile"):
                    raise VerifyError(
                        f"loop variable {name!r} is an index, not a value", node
                    )
                return e.type
            case N.Index(name, entries):
                e = scope.get(name)
                if e is None or e.type is None:
                    raise VerifyError(f"{name!r} is not a tensor or a value", node)
                return N.Type(
                    e.type.dtype, self.view(scope, e.type.shape, entries, node)
                )
        raise VerifyError(f"no operand {x!r}", node)

    def view(self, scope, shape, entries, node) -> tuple:
        out, k = [], 0
        for en in entries:
            if isinstance(en, N.NewAxis):
                out.append(1)
                continue
            if k >= len(shape):
                raise VerifyError(
                    f"{len(entries)} indices into {fmt_shape(shape)}", node
                )
            d = shape[k]
            k += 1
            match en:
                case N.Full():
                    out.append(d)
                case N.At(int() as i):
                    if isinstance(d, int) and not 0 <= i < d:
                        raise VerifyError(f"index {i} of a dimension of {d}", node)
                case N.At(str() as v):
                    e = scope.get(v)
                    if e is None or e.kind not in ("point", "tile"):
                        raise VerifyError(f"{v!r} is not a loop variable", node)
                    if e.kind == "tile" and e.extent != d:
                        raise VerifyError(
                            f"{v!r} tiles {e.extent}, the dimension is {d}", node
                        )
                    if e.kind == "tile":
                        out.append(e.size)
                case N.Range(lo, hi):
                    lo, hi = self.dim(scope, lo, node), self.dim(scope, hi, node)
                    if not (isinstance(lo, int) and isinstance(hi, int)):
                        raise VerifyError("a range is two ints", node)
                    if not 0 <= lo < hi or (isinstance(d, int) and hi > d):
                        raise VerifyError(
                            f"range {lo} : {hi} of a dimension of {d}", node
                        )
                    out.append(hi - lo)
        return tuple(out) + tuple(shape[k:])

    def domain(self, scope, d, node) -> tuple:
        """``(kind, size, extent)`` of a loop variable over `d`."""
        match d:
            case N.Span(lo, hi):
                return (
                    "point",
                    None,
                    (self.dim(scope, lo, node), self.dim(scope, hi, node)),
                )
            case N.Tiles(extent, size):
                ext, sz = self.dim(scope, extent, node), self.dim(scope, size, node)
                if not isinstance(sz, int):
                    raise VerifyError("a tile's size is an int", node)
                if isinstance(ext, int) and ext % sz:
                    raise VerifyError(f"{ext} is not whole tiles of {sz}", node)
                return "tile", sz, ext
        raise VerifyError(f"no domain {d!r}", node)

    # --------------------------------------------------------------- body
    def block(self, scope, body, ctx) -> None:
        """`ctx`: ``where`` (program/fn), ``nexts`` (the enclosing scan's
        carries -> updated, or None), ``top`` (directly in the unit's body)."""
        pending = []
        for k, s in enumerate(body):
            match s:
                case N.Tile(name, value):
                    scope.bind(name, Entry("dim", size=value), s)
                case N.Output(name, t):
                    if ctx["where"] != "program" or not ctx["top"]:
                        raise VerifyError(
                            "an output is declared in a program's body", s
                        )
                    scope.bind(name, Entry("output", self.type(scope, t, s)), s)
                case N.Let():
                    self.let(scope, s)
                case N.Invoke():
                    self.invoke(scope, s)
                case N.Carry(name, _, t):
                    scope.bind(name, Entry("carry", self.type(scope, t, s)), s)
                    pending.append(s)
                case N.Map(vars, inner):
                    sub = Scope(scope)
                    for v, d in vars:
                        kind, size, extent = self.domain(scope, d, s)
                        sub.bind(v, Entry(kind, size=size, extent=extent), s)
                    self.block(sub, inner, ctx | {"top": False, "nexts": None})
                case N.Scan(var, d, inner):
                    sub = Scope(scope)
                    kind, size, extent = self.domain(scope, d, s)
                    sub.bind(var, Entry(kind, size=size, extent=extent), s)
                    nexts = {c.name: 0 for c in pending}
                    self.block(sub, inner, ctx | {"top": False, "nexts": nexts})
                    for name, n in nexts.items():
                        if n != 1:
                            raise VerifyError(f"carry {name!r} is updated {n} times", s)
                    pending = []
                case N.Next(name, value):
                    nexts = ctx["nexts"]
                    if nexts is None or name not in nexts:
                        raise VerifyError(f"{name!r} is not a carry of this scan", s)
                    nexts[name] += 1
                    want = scope.get(name).type.shape
                    got = self.operand(scope, value, s).shape
                    if got != want:
                        raise VerifyError(
                            f"next {name}: {fmt_shape(got)} for {fmt_shape(want)}", s
                        )
                case N.Store(target, value):
                    e = scope.get(target.name)
                    if e is None or e.kind != "output":
                        raise VerifyError(f"{target.name!r} is not an output", s)
                    want = self.operand(scope, target, s).shape
                    got = self.operand(scope, value, s).shape
                    if got != want:
                        raise VerifyError(
                            f"store of {fmt_shape(got)} into {fmt_shape(want)}", s
                        )
                case N.Return(value):
                    if ctx["where"] != "fn" or not ctx["top"] or k != len(body) - 1:
                        raise VerifyError("return ends a function's body", s)
                    got, ret = self.operand(scope, value, s), ctx["ret"]
                    if got != ret:
                        raise VerifyError(
                            f"returns {got.dtype}{fmt_shape(got.shape)}, the function "
                            f"{ret.dtype}{fmt_shape(ret.shape)}",
                            s,
                        )
                case _:
                    raise VerifyError(f"no statement {s!r}", s)
        if pending:
            raise VerifyError("a carry with no scan after it", pending[0])

    def let(self, scope, s: N.Let) -> None:
        op = self.ops.ops.get(s.op)
        if op is None:
            raise VerifyError(f"{self.ops.name} has no op {s.op!r}", s)
        if op.arity is not None and len(s.args) != op.arity:
            raise VerifyError(f"{s.op} takes {op.arity} operands, got {len(s.args)}", s)
        attrs = dict(s.attrs)
        extra = set(attrs) - set(op.attrs)
        if extra:
            raise VerifyError(f"{s.op} has no {', '.join(sorted(extra))}=", s)
        if s.type is None:
            raise VerifyError(f"`{s.name} = {s.op} ...` names its type", s)
        t = self.type(scope, s.type, s)
        if op.dtype is not None and t.dtype != op.dtype:
            raise VerifyError(f"{s.op} gives {op.dtype}, not {t.dtype}", s)
        types = [self.operand(scope, a, s) for a in s.args]
        if op.operands is not None:
            for a, at in zip(s.args, types, strict=True):
                if at.dtype not in op.operands:
                    raise VerifyError(
                        f"{s.op} reads {', '.join(op.operands)}; an operand is {at.dtype}",
                        s,
                    )
        shapes = [at.shape for at in types]
        try:
            got = op.shape(shapes, attrs)
        except ShapeError as e:
            raise VerifyError(f"{s.op}: {e}", s) from None
        if got is not None and tuple(got) != t.shape:
            raise VerifyError(
                f"{s.op} gives {fmt_shape(got)}, the statement says {fmt_shape(t.shape)}",
                s,
            )
        scope.bind(s.name, Entry("value", t), s)

    def invoke(self, scope, s: N.Invoke) -> None:
        fn = self.module.fns.get(s.fn)
        if fn is None:
            raise VerifyError(f"no function {s.fn!r}", s)
        self.function(fn)
        if len(s.args) != len(fn.params):
            raise VerifyError(f"{s.fn} takes {len(fn.params)} operands", s)
        sub: dict = {}
        for (pname, pt), a in zip(fn.params, s.args, strict=True):
            got = self.operand(scope, a, s)
            if got.dtype not in (pt.dtype, "const"):
                raise VerifyError(f"{s.fn}({pname}): {got.dtype} for {pt.dtype}", s)
            if len(got.shape) != len(pt.shape):
                raise VerifyError(
                    f"{s.fn}({pname}): rank {len(got.shape)} for {len(pt.shape)}", s
                )
            for want, have in zip(pt.shape, got.shape, strict=True):
                if isinstance(want, str):
                    if sub.setdefault(want, have) != have:
                        raise VerifyError(
                            f"{s.fn}: {want} is {sub[want]} and {have}", s
                        )
                elif want != have:
                    raise VerifyError(f"{s.fn}({pname}): {fmt_shape(got.shape)}", s)
        ret = N.Type(fn.ret.dtype, tuple(sub.get(d, d) for d in fn.ret.shape))
        if s.type is not None and self.type(scope, s.type, s) != ret:
            raise VerifyError(f"{s.fn} returns {ret.dtype}{fmt_shape(ret.shape)}", s)
        scope.bind(s.name, Entry("value", ret), s)

    # -------------------------------------------------------------- units
    def function(self, fn: N.Fn) -> None:
        if fn.name in self.fn_types:
            return
        if fn.name in self.visiting:
            raise VerifyError(f"{fn.name} calls itself", fn)
        self.visiting.append(fn.name)
        scope = Scope()
        self.bind_symbols(scope, fn.params, fn)
        for n, t in fn.params:
            scope.bind(n, Entry("value", self.type(scope, t, fn)), fn)
        ret = self.type(scope, fn.ret, fn)
        self.block(
            scope, fn.body, {"where": "fn", "top": True, "nexts": None, "ret": ret}
        )
        if not fn.body or not isinstance(fn.body[-1], N.Return):
            raise VerifyError(f"{fn.name} does not end in return", fn)
        self.visiting.pop()
        self.fn_types[fn.name] = ret

    def program(self, p: N.Program) -> None:
        scope = Scope()
        self.bind_symbols(scope, p.params, p)
        for n, t in p.params:
            scope.bind(n, Entry("tensor", self.type(scope, t, p)), p)
        self.block(scope, p.body, {"where": "program", "top": True, "nexts": None})


def verify(module: N.Module, ops, source: str = "", file: str = "<l3>") -> None:
    """Raise `TextError` at the statement of the first problem."""
    v = _Verifier(module, ops)
    try:
        for fn in module.fns.values():
            v.function(fn)
        for p in module.programs.values():
            v.program(p)
    except VerifyError as e:
        line, col = module.positions.get(id(e.node), (0, 0))
        raise TextError(e.message, line, col, source, file) from None


__all__ = ["VerifyError", "verify"]
