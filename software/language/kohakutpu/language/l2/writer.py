"""L2 nodes back to `fn NAME.l2` text, which `reader.body` reads to the same
nodes: the form an L2 -> L2 pass hands to the next stage."""

from kohakutpu.language.l2 import nodes as L
from kohakutpu.language.text.syntax import fmt
from kohakutpu.language.text.writer import Text


def type_text(t: L.Type) -> str:
    dims = ", ".join(str(d) for d in t.shape)
    out = f"{t.dtype}[{dims}]" if t.shape else t.dtype
    if t.space:
        out += f" @{t.space}"
        if t.layout:
            kv = ", ".join(f"{k}={_layout(v)}" for k, v in t.layout)
            out += f"({kv})"
    return out


def _layout(v):
    return "x".join(str(x) for x in v) if isinstance(v, tuple) else str(v)


def axis(a: L.Axis) -> str:
    if a.new:
        return "*"
    if a.lo is None:
        return ":"
    if a.size is None:
        return fmt(a.lo)
    return f"{fmt(a.lo)} : +{a.size}"


def operand(v) -> str:
    if isinstance(v, L.Const):
        return repr(float(v.value))
    if not v.axes:
        return v.name
    return f"{v.name}[{', '.join(axis(a) for a in v.axes)}]"


def op(o: L.Op) -> str:
    attrs = "".join(f" {k}={v}" for k, v in o.attrs)
    return f"{o.op} {', '.join(operand(a) for a in o.args)}{attrs}"


def stmt(out: Text, s) -> None:
    match s:
        case L.Buffer(name, t):
            out(f"{name} = buffer : {type_text(t)}")
        case L.Par(var, lo, hi, unit, at, body):
            out(f"par {var} in {lo}..{hi} on {unit}[{fmt(at)}]")
            with out.block():
                for b in body:
                    stmt(out, b)
        case L.For(var, lo, hi, pipe, body):
            out(f"for {var} in {lo}..{hi}" + (f" pipe={pipe}" if pipe > 1 else ""))
            with out.block():
                for b in body:
                    stmt(out, b)
        case L.Load(name, src, t):
            out(f"{name} = load {operand(src)} : {type_text(t)}")
        case L.Op():
            out(f"{s.name} = {op(s)} : {type_text(s.type)}")
        case L.Store(dst, value):
            v = op(value) if isinstance(value, L.Op) else operand(value)
            out(f"store {operand(dst)} <- {v}")
        case _:
            raise TypeError(f"no L2 text for {s!r}")


def body_text(b: L.Body) -> str:
    """`fn NAME.l2(params)` and its statements."""
    out = Text()
    out(f"fn {b.name}.l2({', '.join(f'{n}: {type_text(t)}' for n, t in b.params)})")
    with out.block():
        for s in b.stmts:
            stmt(out, s)
    return out.text()


__all__ = ["body_text", "type_text"]
