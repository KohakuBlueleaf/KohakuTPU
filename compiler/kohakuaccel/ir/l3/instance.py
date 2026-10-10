"""An L3 program at concrete shapes: what a project's L3 -> L2 lowering reads.

`instantiate` binds every dimension symbol from the inputs' shapes and the
`tile` statements, expands `inline` and `call` alike (a call's result is the
function's, its arguments rounded to its parameters' dtypes), resolves every
operand to a constant, a tensor (a parameter or an output) or a value with its
index entries, and drops the statements nothing stored or carried reads.
Values of an expanded function are named ``site.name``.
"""

import itertools
from dataclasses import dataclass

from kohakuaccel.ir.l3 import nodes as N


class InstanceError(ValueError):
    pass


@dataclass(frozen=True)
class Operand:
    """A constant (`name` None), or a tensor or value with its index entries,
    each ``("full",)``, ``("new",)``, ``("at", i)``, ``("var", name)`` or
    ``("range", lo, hi)``; ``()`` is the whole of it."""

    name: str | None = None
    entries: tuple = ()
    value: float | None = None
    tensor: bool = False


@dataclass(frozen=True)
class Stmt:
    name: str
    op: str
    args: tuple
    attrs: tuple
    dtype: str
    shape: tuple


@dataclass(frozen=True)
class Loop:
    """A map or a scan: ``vars`` ``((name, kind, a, b), ...)``, kind ``"span"``
    (points ``a .. b``) or ``"tiles"`` (``a / b`` tiles of `b`)."""

    kind: str
    vars: tuple
    body: tuple


@dataclass(frozen=True)
class Put:
    target: Operand
    value: Operand


@dataclass(frozen=True)
class CarryInit:
    name: str
    init: float
    dtype: str
    shape: tuple


@dataclass(frozen=True)
class Update:
    name: str
    value: Operand


@dataclass(frozen=True)
class Instance:
    name: str
    #: name -> (dtype, shape), in parameter order
    params: dict
    outputs: dict
    body: tuple
    #: value name -> (dtype, shape)
    values: dict


class _Scope:
    def __init__(self, parent=None, prefix: str = "") -> None:
        self.parent, self.prefix = parent, prefix
        self.dims: dict = {}
        self.names: dict = {}  # source name -> Operand (value, tensor or alias)
        self.loops: dict = {}  # loop var -> (kind, size)

    def find(self, table: str, key):
        s = self
        while s is not None:
            t = getattr(s, table)
            if key in t:
                return t[key]
            s = s.parent
        return None


class _Inst:
    def __init__(self, module: N.Module) -> None:
        self.module = module
        self.values: dict = {}
        self.tensors: dict = {}
        self.sites = itertools.count()

    # ---------------------------------------------------------------- dims
    def dim(self, sc: _Scope, d) -> int:
        if isinstance(d, int):
            return d
        v = sc.find("dims", d)
        if v is None:
            raise InstanceError(f"dimension {d!r} is not bound")
        return v

    def shape(self, sc, t: N.Type) -> tuple:
        return tuple(self.dim(sc, d) for d in t.shape)

    def entry(self, sc, e):
        match e:
            case N.Full():
                return ("full",)
            case N.NewAxis():
                return ("new",)
            case N.At(int() as i):
                return ("at", i)
            case N.At(str() as v):
                if sc.find("loops", v) is None:
                    return ("at", self.dim(sc, v))
                return ("var", v)
            case N.Range(lo, hi):
                return ("range", self.dim(sc, lo), self.dim(sc, hi))
        raise InstanceError(f"index entry {e!r}")

    # ------------------------------------------------------------ operands
    def operand(self, sc, x) -> Operand:
        match x:
            case N.Const(v):
                return Operand(value=float(v))
            case N.Ref(name):
                return self.named(sc, name, ())
            case N.Index(name, entries):
                return self.named(sc, name, tuple(self.entry(sc, e) for e in entries))
        raise InstanceError(f"operand {x!r}")

    def named(self, sc, name, entries) -> Operand:
        o = sc.find("names", name)
        if o is None:
            d = sc.find("dims", name)
            if d is None:
                raise InstanceError(f"{name!r} is not bound")
            if entries:
                raise InstanceError(f"the dimension {name} indexed")
            return Operand(value=float(d))
        if entries:
            if o.entries:
                raise InstanceError(
                    f"{name} is an indexed argument; indexing it again is not "
                    "instantiated"
                )
            return Operand(o.name, entries, None, o.tensor)
        return o

    def dtype_of(self, o: Operand):
        if o.name is None:
            return None
        return (self.tensors if o.tensor else self.values)[o.name][0]

    def shape_of(self, o: Operand, sc) -> tuple:
        if o.name is None:
            return ()
        base = (self.tensors if o.tensor else self.values)[o.name][1]
        out, k = [], 0
        for e in o.entries:
            match e:
                case ("new",):
                    out.append(1)
                    continue
                case ("full",):
                    out.append(base[k])
                case ("at", _):
                    pass
                case ("var", v):
                    kind, size = sc.find("loops", v)
                    if kind == "tiles":
                        out.append(size)
                case ("range", lo, hi):
                    out.append(hi - lo)
            k += 1
        return tuple(out) + tuple(base[k:])

    # -------------------------------------------------------------- bodies
    def block(self, sc, body) -> list:
        out = []
        for s in body:
            match s:
                case N.Tile(name, value):
                    sc.dims[name] = value
                case N.Output(name, t):
                    self.tensors[name] = (t.dtype, self.shape(sc, t))
                    sc.names[name] = Operand(name, (), None, True)
                case N.Let(name, op, args, attrs, t):
                    full = sc.prefix + name
                    self.values[full] = (t.dtype, self.shape(sc, t))
                    sc.names[name] = Operand(full)
                    out.append(
                        Stmt(
                            full,
                            op,
                            tuple(self.operand(sc, a) for a in args),
                            tuple(attrs),
                            t.dtype,
                            self.values[full][1],
                        )
                    )
                case N.Invoke(name, fn, args, _, _t):
                    out += self.invoke(sc, name, self.module.fns[fn], args)
                case N.Carry(name, init, t):
                    full = sc.prefix + name
                    self.values[full] = (t.dtype, self.shape(sc, t))
                    sc.names[name] = Operand(full)
                    out.append(
                        CarryInit(
                            full, float(init.value), t.dtype, self.values[full][1]
                        )
                    )
                case N.Map(vars, inner):
                    out.append(self.loop(sc, "map", vars, inner))
                case N.Scan(var, d, inner):
                    out.append(self.loop(sc, "scan", ((var, d),), inner))
                case N.Next(name, value):
                    out.append(
                        Update(sc.find("names", name).name, self.operand(sc, value))
                    )
                case N.Store(target, value):
                    t = self.named(
                        sc,
                        target.name,
                        tuple(self.entry(sc, e) for e in target.entries),
                    )
                    out.append(Put(t, self.operand(sc, value)))
                case N.Return(value):
                    sc.names["$return"] = self.operand(sc, value)
        return out

    def loop(self, sc, kind, vars, inner) -> Loop:
        sub = _Scope(sc, sc.prefix)
        cvars = []
        for v, d in vars:
            match d:
                case N.Span(lo, hi):
                    cvars.append((v, "span", self.dim(sc, lo), self.dim(sc, hi)))
                    sub.loops[v] = ("span", None)
                case N.Tiles(ext, size):
                    e, z = self.dim(sc, ext), self.dim(sc, size)
                    if e % z:
                        raise InstanceError(f"{v}: {e} is not whole tiles of {z}")
                    cvars.append((v, "tiles", e, z))
                    sub.loops[v] = ("tiles", z)
        return Loop(kind, tuple(cvars), tuple(self.block(sub, inner)))

    def invoke(self, sc, name, f: N.Fn, args) -> list:
        site = f"{sc.prefix}{name}."
        sub = _Scope(None, site)
        out = []
        for (pn, pt), a in zip(f.params, args, strict=True):
            o = self.operand(sc, a)
            have = self.shape_of(o, sc)
            for want, got in zip(pt.shape, have, strict=True):
                if isinstance(want, str):
                    if sub.dims.setdefault(want, got) != got:
                        raise InstanceError(
                            f"{f.name}: {want} is {sub.dims[want]} and {got}"
                        )
                elif want != got:
                    raise InstanceError(f"{f.name}({pn}): {have} for {pt.shape}")
            if o.name is not None and self.dtype_of(o) != pt.dtype:
                full = site + pn
                self.values[full] = (pt.dtype, have)
                out.append(Stmt(full, "copy", (o,), (), pt.dtype, have))
                o = Operand(full)
            sub.names[pn] = o
        # the caller's loop variables size the arguments' tiles; its names and
        # dims stay out of the function
        sub.parent = _Scope(None)
        sub.parent.loops = _all_loops(sc)
        out += self.block(sub, f.body)
        ret = sub.names.get("$return")
        if ret is None:
            raise InstanceError(f"{f.name} returns nothing")
        sc.names[name] = ret
        return out


def _all_loops(sc) -> dict:
    out: dict = {}
    chain = []
    s = sc
    while s is not None:
        chain.append(s)
        s = s.parent
    for s in reversed(chain):
        out.update(s.loops)
    return out


def _live(body, want: set) -> tuple:
    """`body` without the statements nothing in `want` (or a store, an
    update) reads; `want` gains what the kept ones read."""
    out = []
    for s in reversed(body):
        match s:
            case Put(_, v):
                want.add(v.name)
                out.append(s)
            case Update(name, v):
                want |= {name, v.name}
                out.append(s)
            case Stmt(name=name, args=args):
                if name in want:
                    want |= {a.name for a in args}
                    out.append(s)
            case Loop(kind, vars, inner):
                kept = _live(inner, want)
                if kept:
                    out.append(Loop(kind, vars, kept))
            case CarryInit(name=name):
                if name in want:
                    out.append(s)
    return tuple(reversed(out))


def instantiate(module: N.Module, program: str, shapes: dict) -> Instance:
    """`program` at the inputs' `shapes` (name -> shape)."""
    p = module.programs[program]
    inst = _Inst(module)
    sc = _Scope()
    params = {}
    for n, t in p.params:
        if n not in shapes:
            raise InstanceError(f"{p.name} wants {n}")
        have = tuple(shapes[n])
        if len(have) != len(t.shape):
            raise InstanceError(f"{n}: rank {len(have)} for {len(t.shape)}")
        for want, got in zip(t.shape, have, strict=True):
            if isinstance(want, int) and want != got:
                raise InstanceError(f"{n}: {have} for {t.shape}")
            if isinstance(want, str) and sc.dims.setdefault(want, got) != got:
                raise InstanceError(f"{n}: {want} is {sc.dims[want]} and {got}")
        inst.tensors[n] = (t.dtype, have)
        params[n] = inst.tensors[n]
        sc.names[n] = Operand(n, (), None, True)
    body = inst.block(sc, p.body)
    body = _live(body, set())
    outputs = {n: v for n, v in inst.tensors.items() if n not in params}
    return Instance(p.name, params, outputs, body, dict(inst.values))


__all__ = [
    "CarryInit",
    "Instance",
    "InstanceError",
    "Loop",
    "Operand",
    "Put",
    "Stmt",
    "Update",
    "instantiate",
]
