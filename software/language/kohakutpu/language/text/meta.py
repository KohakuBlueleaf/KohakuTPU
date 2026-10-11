"""Read-time expansion of a body (docs/spec/ir-text.md §2): `for .. unroll`, `when`,
`expand MACRO(args)`, computed names (`v{i+1}`) and constant arithmetic.

The result holds no meta statement and no expression a constant can fold: a
level reader sees instructions only. A name the environment does not bind
stays a name (a buffer, a register); `buf + 4K` with `buf` free stays an
`Offset` of the name and one integer.
"""

from dataclasses import replace

from kohakutpu.language.text.syntax import (
    AddAssign,
    Arrow,
    Assign,
    BinOp,
    Call,
    Compare,
    Computed,
    Dims,
    Float,
    Int,
    Kw,
    Member,
    Name,
    Neg,
    NewAxis,
    Not,
    Offset,
    Placed,
    Pos,
    Slice,
    Span,
    Stmt,
    Str,
    Tuple,
    Typed,
    View,
)

META = ("for", "when", "expand")


class Expander:
    """Expands statements against `macros` (name -> (params, body)); `fail`
    reports at a statement. `unroll_all` makes every `for` unroll (an L1 body
    has no loop the hardware runs except `loop`)."""

    def __init__(self, macros: dict, fail, unroll_all: bool = False) -> None:
        self.macros = macros
        self.fail = fail
        self.unroll_all = unroll_all
        self._depth = 0

    # ------------------------------------------------------------ statements
    def stmts(self, stmts: list, env: dict) -> list:
        out = []
        for st in stmts:
            out += self.stmt(st, env)
        return out

    def stmt(self, st: Stmt, env: dict) -> list:
        if st.op == "for" and st.target is None and self._unrolls(st):
            return self._for(st, env)
        if st.op == "when" and st.target is None:
            (cond,) = st.positional() or [None]
            if cond is None:
                self.fail(st, "wanted `when CONDITION`")
            v = self.term(cond, env, st)
            if not isinstance(v, Int):
                self.fail(st, f"`when` wants a constant condition, got {v!r}")
            return self.stmts(st.body, env) if v.value else []
        if st.op == "expand" and st.target is None:
            return self._expand(st, env)
        args = [self.term(a, env, st) for a in st.args]
        annot = None if st.annot is None else self.term(st.annot, env, st)
        # A macro parameter bound to a name may stand for the op itself.
        op = env[st.op].text if isinstance(env.get(st.op), Name) else st.op
        target = st.target
        if isinstance(target, Computed):
            target = self.term(target, env, st).text
        elif isinstance(env.get(target), Name):
            target = env[target].text
        return [
            replace(
                st,
                op=op,
                target=target,
                args=args,
                annot=annot,
                body=self.stmts(st.body, env),
            )
        ]

    def _unrolls(self, st: Stmt) -> bool:
        pos = st.positional()
        return self.unroll_all or Name("unroll") in pos[1:]

    def _for(self, st: Stmt, env: dict) -> list:
        pos = st.positional()
        if (
            not pos
            or not isinstance(pos[0], Member)
            or not isinstance(pos[0].lhs, Name)
        ):
            self.fail(st, "wanted `for VAR in LO..HI [unroll]`")
        dom = self.term(pos[0].rhs, env, st)
        if not isinstance(dom, Span) or not all(
            isinstance(x, Int) for x in (dom.lo, dom.hi)
        ):
            self.fail(st, f"an unrolled `for` needs constant bounds, got {dom!r}")
        out = []
        var = pos[0].lhs.text
        for v in range(dom.lo.value, dom.hi.value):
            out += self.stmts(st.body, {**env, var: v})
        return out

    def _expand(self, st: Stmt, env: dict) -> list:
        (call,) = st.positional() or [None]
        if not isinstance(call, Call) or call.name not in self.macros:
            self.fail(st, f"wanted `expand MACRO(args)` naming a macro, got {call!r}")
        params, body = self.macros[call.name]
        if len(call.args) != len(params):
            self.fail(st, f"{call.name} takes {len(params)} arguments")
        self._depth += 1
        if self._depth > 32:
            self.fail(st, f"macro {call.name} expands itself")
        inner = {**env}
        for p, a in zip(params, call.args, strict=True):
            inner[p] = _value(self.term(a, env, st))
        out = self.stmts(body, inner)
        self._depth -= 1
        return out

    # ----------------------------------------------------------------- terms
    def term(self, t, env: dict, st):
        match t:
            case Name(text) if text in env:
                return _lit(env[text])
            case Name() | Int() | Float() | Str() | Dims() | Arrow() | NewAxis():
                return t
            case Neg(x):
                v = self.term(x, env, st)
                if isinstance(v, Int):
                    return Int(-v.value)
                if isinstance(v, Float):
                    return Float(-v.value)
                return Neg(v)
            case Not(x):
                v = self.term(x, env, st)
                return Int(int(not v.value)) if isinstance(v, Int) else Not(v)
            case Pos(x):
                return Pos(self.term(x, env, st))
            case Offset(a, b):
                return _add(self.term(a, env, st), self.term(b, env, st))
            case BinOp(op, a, b):
                return self._bin(op, self.term(a, env, st), self.term(b, env, st), st)
            case Compare(op, a, b):
                x, y = self.term(a, env, st), self.term(b, env, st)
                if isinstance(x, (Int, Float)) and isinstance(y, (Int, Float)):
                    return Int(int(_CMP[op](x.value, y.value)))
                return Compare(op, x, y)
            case Computed(prefix, index, slices):
                i = self.term(index, env, st)
                if not isinstance(i, Int):
                    self.fail(st, f"{prefix}{{...}} wants a constant index, got {i!r}")
                name = f"{prefix}{i.value}"
                if slices is None:
                    return Name(name)
                return View(name, tuple(self.term(s, env, st) for s in slices))
            case Span(lo, hi):
                return Span(self.term(lo, env, st), self.term(hi, env, st))
            case Slice(lo, hi):
                return Slice(
                    None if lo is None else self.term(lo, env, st),
                    None if hi is None else self.term(hi, env, st),
                )
            case View(name, slices):
                return View(name, tuple(self.term(s, env, st) for s in slices))
            case Call(name, args):
                return Call(name, tuple(self.term(a, env, st) for a in args))
            case Tuple(items):
                return Tuple(tuple(self.term(x, env, st) for x in items))
            case Typed(name, ty):
                return Typed(name, self.term(ty, env, st))
            case Kw(name, v):
                return Kw(name, self.term(v, env, st))
            case Placed(x, space):
                return Placed(self.term(x, env, st), self.term(space, env, st))
            # A keyword or a register named on the left is a name, never a value.
            case Assign(a, b):
                lhs = a if isinstance(a, Name) else self.term(a, env, st)
                return Assign(lhs, self.term(b, env, st))
            case AddAssign(a, b):
                lhs = a if isinstance(a, Name) else self.term(a, env, st)
                return AddAssign(lhs, self.term(b, env, st))
            case Member(a, b):
                return Member(a, self.term(b, env, st))
        return t

    def _bin(self, op, a, b, st):
        if isinstance(a, (Int, Float)) and isinstance(b, (Int, Float)):
            x, y = a.value, b.value
            if op in "/%" and y == 0:
                self.fail(st, "division by zero")
            if op == "/" and isinstance(a, Int) and isinstance(b, Int):
                return Int(x // y)
            v = _ARITH[op](x, y)
            return Int(v) if isinstance(v, int) else Float(v)
        if op == "-" and isinstance(b, Int):
            return _add(a, Int(-b.value))
        return BinOp(op, a, b)


_ARITH = {
    "-": lambda x, y: x - y,
    "*": lambda x, y: x * y,
    "/": lambda x, y: x / y,
    "%": lambda x, y: x % y,
}

_CMP = {
    "==": lambda x, y: x == y,
    "!=": lambda x, y: x != y,
    "<": lambda x, y: x < y,
    "<=": lambda x, y: x <= y,
    ">": lambda x, y: x > y,
    ">=": lambda x, y: x >= y,
}


def _lit(v):
    if isinstance(v, bool):
        return Int(int(v))
    if isinstance(v, int):
        return Int(v)
    if isinstance(v, float):
        return Float(v)
    return v


def _value(t):
    """A macro argument: a constant becomes a Python number, anything else
    (a register name) stays a term."""
    if isinstance(t, Int):
        return t.value
    if isinstance(t, Float):
        return t.value
    return t


def _add(a, b):
    """`a + b`, folded: two constants add; a symbol plus constants keeps one
    constant offset."""
    if isinstance(a, (Int, Float)) and isinstance(b, (Int, Float)):
        v = a.value + b.value
        return Int(v) if isinstance(v, int) else Float(v)
    if isinstance(a, Offset) and isinstance(a.off, Int) and isinstance(b, Int):
        return _add(a.base, Int(a.off.value + b.value))
    if isinstance(b, Int) and b.value == 0:
        return a
    if isinstance(a, Int) and not isinstance(b, Int):
        return _add(b, a)
    return Offset(a, b)


def macro_params(fail, st, call) -> tuple:
    """The parameter names of `macro NAME(a, b)` / `image NAME(a)`."""
    out = []
    for p in call.args if isinstance(call, Call) else ():
        if not isinstance(p, Name):
            fail(st, f"a parameter is a name, got {p!r}")
        out.append(p.text)
    return tuple(out)


__all__ = ["META", "Expander", "macro_params"]
