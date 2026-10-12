"""The kernel surface: each function records one or a few L2 statements.

Control: `par`, `range`, `when`, `sticky`. Cluster: `fill`, `zero`, `gemm`,
`drain`, `recv`. Vector: `load`, `unpack`, `store`, `send`, `recv`, the math
(`math` behind every operator and the named functions), `reduce`. Node scope:
the mover (`copy`, `quant`, `gt4`, `fill_value`, `gen`, `gather`,
`scatter`) and `buffer` (scratch memory).
"""

import contextlib
import numbers
from collections.abc import Iterator
from typing import cast

from kohakutpu.language.dsl import expr as E
from kohakutpu.language.dsl import values as V
from kohakutpu.language.dsl.errors import DSLError, at_caller
from kohakutpu.language.dsl.trace import Scope, active
from kohakutpu.language.dsl.types import TensorSpec
from kohakutpu.language.dsl.values import Param, Unit, Value, ValueView, as_view

#: L2 vector ops by arity (`compiler.l2.vector`), and the comparisons.
UNARY = ("exp2", "inv", "rsqrt", "log2", "neg", "abs", "mov")
BINARY = ("add", "sub", "mul", "max", "min", "exp2d", "xlane")
TERNARY = ("fma", "fnma", "sel")
COMPARE = ("cmplt", "cmpgt", "cmpeq")
REDUCE = ("rmax", "rsum")


# ----------------------------------------------------------------- control
def _domain(n, lo):
    """``(lo, hi)`` of a `par` / `range`: `range(4)`, `range(2, 6)`."""
    return (0, n) if lo is None else (n, lo)


def par(
    n: E.Index,
    hi: E.Index | None = None,
    *,
    on: Unit | None = None,
    at=None,
    name: str | None = None,
) -> Iterator[E.Sym]:
    """Instances run concurrently, one a value of the variable: ``for c in
    kt.par(2, on=kt.vc)`` is ``par c in 0..2 on vc[c]``. `at` names the
    unit (an expression of the variable, or `kt.ANY` for the place pass).

    More `mg` / `vc` instances than a node has, outside a par over nodes,
    span the machine's nodes: the nodes look like one node of all their
    units (``par n on node[n]`` around ``par u on mg[u]``, the variable
    ``n * per + u``); their memory is one through the Xache."""
    t = active()
    if not isinstance(on, Unit) or on.index is not None or on.node is not None:
        raise DSLError(f"par runs on=kt.mg, kt.vc or kt.node, at {at_caller()}")
    if t.scope.kind is not None:
        raise DSLError(f"a par inside a unit's instance, at {at_caller()}")
    lo, hi = _domain(n, hi)
    m = t.machine
    per = {"mg": m.mg, "vc": m.vc}.get(on.kind or "")
    # `lo == 0` on a symbol is a DSL condition, not a bool: compare ints only.
    top = hi if isinstance(hi, int) else None
    first = lo if isinstance(lo, int) else None
    wide = top is not None and per is not None and top > per and first == 0
    if wide and m.nodes > 1 and not t.scope.node:
        assert top is not None and per is not None
        yield from _over_nodes(t, on, top, per, at, name)
        return
    v = t.claim(name) if name else _var_name(t, on.kind)
    sym = E.var(v)
    where = sym if at is None else at
    unit = f"{on.kind}[any]" if where is V.ANY else f"{on.kind}[{E.text(where)}]"
    st = t.emit("par", f"{v} in {E.text(lo)}..{E.text(hi)}", "on", unit)
    kind = None if on.kind == "node" else on.kind
    with t.block(st, Scope(kind=kind, node=on.kind == "node" or t.scope.node)):
        yield sym


def _over_nodes(t, on: Unit, count: int, per: int, at, name) -> Iterator[E.Sym]:
    """`count` instances of `on` spread over the nodes: `per` a node on
    their own units, or (``at=kt.ANY``) an equal share a node, placed by the
    place pass."""
    nodes = t.machine.nodes
    if at is not None and at is not V.ANY:
        raise DSLError(
            f"a par over the nodes' units places by kt.ANY or itself, at {at_caller()}"
        )
    if at is None:
        share, used = per, count // per
        ok = count % per == 0 and used <= nodes
    else:
        share, used = count // nodes, nodes
        ok = count % nodes == 0
    if not ok:
        raise DSLError(
            f"{count} {on.kind} instances do not split evenly over {nodes} nodes of "
            f"{per}, at {at_caller()}"
        )
    nv = _var_name(t, "node")
    nst = t.emit("par", f"{nv} in 0..{used}", "on", f"node[{nv}]")
    with t.block(nst, Scope(kind=None, node=True)):
        v = t.claim(name) if name else _var_name(t, on.kind)
        unit = f"{on.kind}[any]" if at is V.ANY else f"{on.kind}[{v}]"
        st = t.emit("par", f"{v} in 0..{share}", "on", unit)
        with t.block(st, Scope(kind=on.kind)):
            yield cast(E.Sym, E.var(nv) * share + E.var(v))


def range(
    n: E.Index,
    hi: E.Index | None = None,
    *,
    pipe: int | None = None,
    name: str | None = None,
) -> Iterator[E.Sym]:
    """Ordered iterations recorded ONCE with a symbolic variable: ``for t in
    kt.range(32)`` is ``for t in 0..32``. On a vector core it is the stream
    loop (a hardware loop); at node scope `pipe=K` lets K iterations run
    ahead. A Python ``range`` unrolls instead."""
    t = active()
    lo, hi = _domain(n, hi)
    v = t.claim(name) if name else t.fresh("t" if t.scope.kind == "vc" else "k")
    st = t.emit("for", f"{v} in {E.text(lo)}..{E.text(hi)}", kw={"pipe": pipe})
    scope = Scope(kind=t.scope.kind, loop=True, sticky=t.scope.sticky)
    with t.block(st, scope):
        yield E.var(v)


@contextlib.contextmanager
def when(cond: E.Cond | bool) -> Iterator[None]:
    """A block kept where a constant-per-instance condition holds: ``with
    kt.when(u == 0):``. A Python bool is a Python `if`."""
    t = active()
    if isinstance(cond, bool):
        raise DSLError(f"kt.when of a Python bool: write `if`, at {at_caller()}")
    if not isinstance(cond, E.Cond):
        raise DSLError(f"kt.when takes an index comparison, at {at_caller()}")
    st = t.emit("when", cond.text())
    with t.block(st):
        yield


@contextlib.contextmanager
def sticky() -> Iterator[None]:
    """A block of vector math run only when an `exp2d` overflowed (the
    core's sticky flag); a value it rebinds (`Value.assign`) keeps its old
    contents when skipped."""
    t = active()
    if t.scope.kind != "vc" or not t.scope.loop:
        raise DSLError(
            f"kt.sticky is a vector core's, in its stream loop, at {at_caller()}"
        )
    st = t.emit("if", "sticky")
    with t.block(st, Scope(kind="vc", loop=True, sticky=True)):
        yield


def require(cond: bool, why: str) -> None:
    """A compile-time precondition: refuses with `why` when false."""
    if not isinstance(cond, bool):
        raise DSLError(f"kt.require takes a Python bool, at {at_caller()}")
    if not cond:
        raise DSLError(f"{why}, at {at_caller()}")


def _var_name(t, kind: str | None) -> str:
    return t.fresh({"mg": "u", "vc": "c", "node": "n"}[kind or "node"])


# --------------------------------------------------------------- buffers
def buffer(shape, dtype="f16", *, space="dram", name=None, **layout) -> Param:
    """Scratch memory, declared at the top of the body: ``h = buffer :
    f16[256, 256] @dram(tile=64x64)``. ``space="any"`` lets the space pass
    route a drain straight into a core."""
    t = active()
    spec = TensorSpec.make(shape, dtype, layout, space)
    n = t.claim(name) if name else t.fresh("h")
    t.scratch[n] = spec
    return Param(n, spec)


# ---------------------------------------------------------------- cluster
def _need(kind: str | None, what: str) -> None:
    t = active()
    if t.scope.kind != kind:
        where = {None: "node scope", "mg": "a cluster", "vc": "a vector core"}
        raise DSLError(
            f"{what} runs on {where[kind]}, not in {where[t.scope.kind]}, at {at_caller()}"
        )


def _unit() -> Unit:
    return Unit(active().scope.kind)


def _value(op, args, dtype, shape, space, mode=None, prefix="v", kw=None, after=None):
    """A new value made by one statement; `after` puts the statement just
    behind that value's own (an unpack of a value loaded outside the loop
    lands beside its load, not in every trip)."""
    t = active()
    name = t.fresh(prefix)
    v = Value(t, name, dtype, shape, space, mode, _unit())
    v.stmt = t.emit(op, *args, target=name, annot=v.type_text(), kw=kw)
    v.block = t.stack[-1]
    if after is not None and after.block is not t.stack[-1]:
        t.stack[-1].pop()
        after.block.insert(after.block.index(after.stmt) + 1, v.stmt)
        v.block = after.block
    return v


def fill(view, *, share=None) -> Value:
    """An MXFP7 operand into a cluster bank: ``a = fill x[...] : mx7a[M, K]
    @A`` (an mx7b view fills @B). `share` = port | all | cross."""
    _need("mg", "fill")
    v = as_view(view)
    side = {"mx7a": "A", "mx7b": "B"}.get(v.param.dtype)
    if side is None:
        raise DSLError(
            f"fill reads an mx7a or mx7b buffer, not {v.param.dtype}, at {at_caller()}"
        )
    return _value(
        "fill",
        [v.text()],
        v.param.dtype,
        v.shape,
        side,
        prefix=side.lower(),
        kw={"share": share},
    )


def zero(m: int, n: int) -> Value:
    """An accumulator tile the next gemm into it opens: ``acc = zero :
    f32[M, N] @acc``."""
    _need("mg", "zero")
    return _value("zero", [], "f32", (m, n), "acc", prefix="acc")


def gemm(a, b, acc=None) -> Value:
    """One sweep: ``acc = gemm a, b, acc`` chains into `acc` (the name is
    rebound, so a loop carries it); without `acc` a new tile."""
    _need("mg", "gemm")
    srcs, rows = [], []
    for x, side in ((a, "A"), (b, "B")):
        base = x.value if isinstance(x, ValueView) else x
        if not isinstance(base, Value) or base.space != side:
            raise DSLError(f"gemm reads an @A value and an @B value, at {at_caller()}")
        srcs.append(x.text() if isinstance(x, ValueView) else x.name)
        rows.append(x.shape[0])
    shape = (rows[0], rows[1])
    t = active()
    if acc is not None:
        if not isinstance(acc, Value) or acc.space != "acc" or acc.shape != shape:
            raise DSLError(
                f"gemm accumulates into an f32{list(shape)} @acc value, at {at_caller()}"
            )
        v = Value(t, acc.name, "f32", shape, "acc", None, _unit())
        v.stmt = t.emit("gemm", *srcs, acc.name, target=acc.name, annot=v.type_text())
        return v
    return _value("gemm", srcs, "f32", shape, "acc", prefix="acc")


def drain(acc, dst, *, add=False) -> None:
    """The tile out: to a memory view (an f16 ``tile=`` buffer), into a
    core's L1 (`kt.vc[h]`, a `recv` there) or added into another cluster's
    accumulator (`kt.mg[j]`, ``add=True``)."""
    _need("mg", "drain")
    if not isinstance(acc, Value) or acc.space != "acc":
        raise DSLError(f"drain takes an @acc value, at {at_caller()}")
    to = dst.text() if isinstance(dst, Unit) else as_view(dst).text()
    args = [acc.name, "->", to] + (["add"] if add else [])
    far = dst.node if isinstance(dst, Unit) else None
    if far is not None and not (add and dst.kind == "mg"):
        raise DSLError(
            f"to another node's unit a tile is added: kt.drain(acc, kt.node[N].mg[J], "
            f"add=True), at {at_caller()}"
        )
    active().emit("drain", *args, kw={"node": None if far is None else E.text(far)})


def recv(src: Unit, shape, *, side=None, gt4=True) -> Value:
    """What another unit sent here. On a core: ``h = recv mg[U] : f16[R, C]
    @l1(gt4)`` (a cluster's drained sub-tiles); on a cluster: ``a = recv
    vc[H] : mx7a[M, K] @A`` (a core's packed rows, `side` A or B)."""
    t = active()
    if t.scope.kind == "vc":
        return _value(
            "recv", [src.text()], "f16", shape, "l1", "gt4" if gt4 else None, prefix="h"
        )
    _need("mg", "recv")
    side = side or "A"
    dtype = {"A": "mx7a", "B": "mx7b"}[side]
    return _value("recv", [src.text()], dtype, shape, side, prefix=side.lower())


# ----------------------------------------------------------------- vector
def load(view, *, gt4=None) -> Value:
    """A block of memory into this core's L1: ``xl = load x[...] : f16[R, C]
    @l1``. A ``tile=`` buffer loads as GT4 bands; ``gt4=True`` reads a
    row-major view as 4-row bands too (`@l1(gt4)`), and a one-row view so is
    a row broadcast (one band every row of which reads it); a 1-D view
    without gt4 is a row statistic once unpacked."""
    _need("vc", "load")
    v = as_view(view)
    spec = v.param.spec
    if spec.dtype not in ("f16", "f32"):
        raise DSLError(f"a core loads f16 or f32, not {spec.dtype}, at {at_caller()}")
    mode = "gt4" if (spec.tiled or gt4) else None
    shape = v.shape
    if gt4 and not spec.tiled and (len(shape) == 1 or shape[0] == 1):
        shape = (4, shape[-1])
    out = _value("load", [v.text()], spec.dtype, shape, "l1", mode, prefix="xl")
    out.tiled = spec.tiled
    return out


def unpack(x) -> Value:
    """An @l1 value (or a band of one: ``h[t*4 : +4, :]``) into registers:
    f32, one row a register (GT4: a 4 x 32 block a register); a 1-D value
    becomes a row statistic."""
    _need("vc", "unpack")
    base = x.value if isinstance(x, ValueView) else x
    if not isinstance(base, Value) or base.space != "l1":
        raise DSLError(f"unpack takes an @l1 value, at {at_caller()}")
    if isinstance(x, Value) and x.unpacked is not None:
        return x.unpacked
    src = x.text() if isinstance(x, ValueView) else x.name
    after = x if isinstance(x, Value) else None
    out = _value(
        "unpack", [src], "f32", x.shape, "reg", base.mode, prefix="v", after=after
    )
    if isinstance(x, Value):
        x.unpacked = out
    return out


def _reg(x):
    """`x` as a register operand: a number stays a number, an @l1 value is
    unpacked here."""
    if isinstance(x, numbers.Real) and not isinstance(x, bool):
        return float(x)
    if isinstance(x, ValueView):
        return x
    if not isinstance(x, Value):
        raise DSLError(
            f"vector math takes values and numbers, got {type(x).__name__}, at {at_caller()}"
        )
    if x.space == "l1":
        return unpack(x)
    if x.space != "reg":
        raise DSLError(
            f"{x.name} is @{x.space}: a cluster's, not a core's register value, at {at_caller()}"
        )
    return x


def math(op: str, *args: V.Operand) -> Value:
    """One L2 vector op over values and numbers. A row value broadcasts a
    statistic (`m[:, *]`); all statistics give a statistic."""
    _need("vc", op)
    want = 1 if op in UNARY else 2 if op in BINARY or op in COMPARE else 3
    if op not in UNARY + BINARY + TERNARY + COMPARE:
        raise DSLError(f"no vector op {op!r}, at {at_caller()}")
    if len(args) != want:
        raise DSLError(f"{op} takes {want} operands, got {len(args)}, at {at_caller()}")
    regs = [_reg(a) for a in args]
    vals = [r for r in regs if isinstance(r, Value)]
    if not vals:
        raise DSLError(f"{op} of numbers alone: fold it in Python, at {at_caller()}")
    rows = [v for v in vals if not v.stat]
    if rows:
        if all(len(v.shape) == 2 for v in rows):
            # A 4-row band broadcasts down the rows, a 32-wide band across them.
            shape = (max(v.shape[0] for v in rows), max(v.shape[1] for v in rows))
        else:
            shape = max((v.shape for v in rows), key=lambda s: (s[0] * s[-1], s))
        modes = {v.mode for v in rows}
        if len(modes) > 1:
            raise DSLError(f"{op} mixes GT4 and plain rows, at {at_caller()}")
        mode = modes.pop()
        texts = [_operand(r, broadcast=True) for r in regs]
    else:
        if op in COMPARE or op == "sel":
            raise DSLError(f"{op} runs on rows, not statistics, at {at_caller()}")
        shape, mode = vals[0].shape, None
        texts = [_operand(r, broadcast=False) for r in regs]
    return _value(op, texts, "f32", shape, "reg", mode, prefix=_PREFIX.get(op, "e"))


_PREFIX = {"cmplt": "m", "cmpgt": "m", "cmpeq": "m", "sel": "o"}


def _operand(r, broadcast: bool) -> str:
    if isinstance(r, float):
        return repr(r)
    if isinstance(r, ValueView):
        return r.text()
    if broadcast and r.stat:
        return f"{r.name}[:, *]"
    return r.name


def reduce(op: str, x) -> Value:
    """A row reduction into a statistic: ``m = rmax e : f32[R] @reg``;
    ``x[:, C0:C1]`` reduces those 32-column blocks of GT4 rows."""
    _need("vc", op)
    if op not in REDUCE:
        raise DSLError(f"no reduction {op!r}: {', '.join(REDUCE)}, at {at_caller()}")
    base = x.value if isinstance(x, ValueView) else _reg(x)
    if not isinstance(base, Value) or base.space != "reg" or base.stat:
        raise DSLError(f"{op} reduces register rows, at {at_caller()}")
    src = x.text() if isinstance(x, ValueView) else base.name
    return _value(op, [src], "f32", (base.shape[0],), "reg", prefix="s")


def store(view, x) -> None:
    """A register value (or an @l1 one) to memory: packs it to the buffer's
    element type (f16, f32, or MXFP7 for a cluster operand), then ``store
    VIEW <- p``. A GT4 statistic packs as f16[R, 32] (its row's value in all
    32 columns)."""
    _need("vc", "store")
    v = as_view(view)
    spec = v.param.spec
    if (
        isinstance(x, Value)
        and x.space == "l1"
        and x.tiled == spec.tiled
        and x.dtype == spec.dtype
    ):
        # Bytes as they were loaded: the same layout and element type.
        p = x
    else:
        r = _reg(x)
        if not isinstance(r, Value):
            raise DSLError(f"store a value, not a number, at {at_caller()}")
        band = r.stat and len(v.shape) == 2 and v.shape[-1] == 32
        p = pack(r, spec.dtype, gt4=r.mode == "gt4" or band)
    active().emit("store", v.text(), "<-", p.name)


def pack(x: Value, dtype: str = "f16", *, gt4=None) -> Value:
    """Registers to L1 as `dtype`: ``p = pack o : f16[R, C] @l1``."""
    _need("vc", "pack")
    if dtype in ("mx7a", "mx7b"):
        if x.mode != "gt4":
            raise DSLError(
                f"an MXFP7 pack takes GT4 rows (4 x 32 blocks), at {at_caller()}"
            )
        return _value("pack", [x.name], dtype, x.shape, "l1", prefix="q")
    if x.stat:
        if not gt4:
            raise DSLError(
                f"a statistic stores from GT4 rows, as f16[R, 32] (its value in all 32 "
                f"columns), at {at_caller()}"
            )
        return _value(
            "pack", [x.name], dtype, (x.shape[0], 32), "l1", "gt4", prefix="p"
        )
    mode = "gt4" if (gt4 or x.mode == "gt4") else None
    return _value("pack", [x.name], dtype, x.shape, "l1", mode, prefix="p")


def send(x: Value, dst: Unit, *, side="A") -> None:
    """Packed MXFP7 rows into a cluster's bank (a `recv` there)."""
    _need("vc", "send")
    if not isinstance(x, Value) or x.space != "l1" or x.dtype not in ("mx7a", "mx7b"):
        raise DSLError(f"a core sends a packed MXFP7 value, at {at_caller()}")
    active().emit("send", x.name, "->", dst.text(), kw={"side": side})


# --------------------------------------------------------------- node scope
def _mover(op, dst, src=None, **kw) -> None:
    _need(None, op)
    args = [as_view(dst).text()]
    if src is not None:
        args += ["<-", as_view(src).text()]
    flags = [k for k, v in kw.items() if v is True]
    kws = {
        k: v
        for k, v in kw.items()
        if v is not True and v is not False and v is not None
    }
    active().emit(op, *args, *flags, kw=kws)


def _walk_text(walk) -> str | None:
    if walk is None:
        return None
    return (
        "("
        + ", ".join(f"({int(n)}, {int(s)})" for n, s in walk)
        + ("," if len(walk) == 1 else "")
        + ")"
    )


def copy(dst, src, *, perm=None, walk=None, at=None, dwalk=None, dat=None) -> None:
    """Words of `src` into `dst` by the mover; `perm` reorders the source's
    word axes. `walk` (``[(count, byte stride), ...]``, outermost first,
    signed) walks the whole buffer `src` from byte `at` instead; `dwalk` /
    `dat` the whole buffer `dst` (a window of it)."""
    perm_text = (
        None
        if perm is None
        else "("
        + ", ".join(str(p) for p in perm)
        + ("," if len(perm) == 1 else "")
        + ")"
    )
    if (walk is not None and not isinstance(src, Param)) or (
        dwalk is not None and not isinstance(dst, Param)
    ):
        raise DSLError(f"a walk copy walks a whole buffer, at {at_caller()}")
    _mover(
        "copy",
        dst,
        src,
        perm=perm_text,
        walk=_walk_text(walk),
        at=None if at is None else int(at),
        dwalk=_walk_text(dwalk),
        dat=None if dat is None else int(dat),
    )


def quant(dst, src) -> None:
    """Row-major f16 into an mx7a / mx7b packing, by the mover's quantiser."""
    _mover("quant", dst, src)


def gt4(dst, src, *, walk=None, at=None, dwalk=None, dat=None) -> None:
    """Row-major f16 into a ``tile=`` buffer's 4 x 4 sub-tiles. With walks
    (whole buffers): each group of four source words (`walk`'s innermost
    level) to four consecutive words, word i = 8-byte granule i of each,
    the destination stepping by `dwalk` once a group."""
    if (walk is None) != (dwalk is None):
        raise DSLError(f"gt4 takes both walks or neither, at {at_caller()}")
    if walk is not None and not (isinstance(src, Param) and isinstance(dst, Param)):
        raise DSLError(f"a walk gt4 walks whole buffers, at {at_caller()}")
    _mover(
        "gt4",
        dst,
        src,
        walk=_walk_text(walk),
        at=None if at is None else int(at),
        dwalk=_walk_text(dwalk),
        dat=None if dat is None else int(dat),
    )


def fill_value(dst, value: int, *, ew=None, walk=None, at=None) -> None:
    """The 32-bit immediate (or its low `ew` bits replicated) over `dst`;
    `walk` / `at` as `copy`'s, over the whole buffer `dst`."""
    if walk is not None and not isinstance(dst, Param):
        raise DSLError(f"a walk fill walks a whole buffer, at {at_caller()}")
    _mover(
        "fill",
        dst,
        value=value,
        ew=ew,
        walk=_walk_text(walk),
        at=None if at is None else int(at),
    )


def gen(dst, seed: int) -> None:
    """Address-keyed pseudo-random words over `dst`."""
    _mover("gen", dst, seed=seed)


def gather(dst, src, index, *, fp=False) -> None:
    """Row r of `dst` := row index[r] of `src`."""
    _mover("gather", dst, src, index=as_view(index).text(), fp=fp)


def scatter(dst, src, index, *, fp=False) -> None:
    """Row index[r] of `dst` := row r of `src`."""
    _mover("scatter", dst, src, index=as_view(index).text(), fp=fp)


V.bind(math=math, reduce=reduce, unpack=unpack)


# ------------------------------------------------------------ named math
def exp2(x: V.Operand) -> Value:
    return math("exp2", x)


def log2(x: V.Operand) -> Value:
    return math("log2", x)


def recip(x: V.Operand) -> Value:
    return math("inv", x)


def rsqrt(x: V.Operand) -> Value:
    return math("rsqrt", x)


def absolute(x: V.Operand) -> Value:
    return math("abs", x)


def maximum(a: V.Operand, b: V.Operand) -> Value:
    return math("max", a, b)


def minimum(a: V.Operand, b: V.Operand) -> Value:
    return math("min", a, b)


def exp2d(a: V.Operand, b: V.Operand) -> Value:
    """``2^(a - floor(b))`` (sets the sticky flag past 2^8)."""
    return math("exp2d", a, b)


def fma(a: V.Operand, b: V.Operand, c: V.Operand) -> Value:
    """``a * b + c``."""
    return math("fma", a, b, c)


def fnma(a: V.Operand, b: V.Operand, c: V.Operand) -> Value:
    """``c - a * b``."""
    return math("fnma", a, b, c)


def where(m: V.Operand, a: V.Operand, b: V.Operand) -> Value:
    """`a` where `m` is not 0, else `b` (one `sel`)."""
    return math("sel", m, a, b)


def xlane(x: V.Operand, k: int) -> Value:
    """Lane l of every 16-lane chunk takes lane ``l xor k`` of `x` (the
    crossbar, exact); with `where` over a lane mask, any in-chunk swap."""
    if not isinstance(k, int) or not 0 < k < 16:
        raise DSLError(f"xlane takes a lane xor 1..15, got {k!r}, at {at_caller()}")
    return math("xlane", x, float(k))


def rmax(x: Value | ValueView) -> Value:
    return reduce("rmax", x)


def rsum(x: Value | ValueView) -> Value:
    return reduce("rsum", x)


__all__ = [
    "absolute",
    "buffer",
    "copy",
    "drain",
    "exp2",
    "exp2d",
    "fill",
    "fill_value",
    "fma",
    "fnma",
    "gather",
    "gemm",
    "gen",
    "gt4",
    "load",
    "log2",
    "math",
    "maximum",
    "minimum",
    "pack",
    "par",
    "quant",
    "range",
    "recip",
    "recv",
    "reduce",
    "require",
    "rmax",
    "rsqrt",
    "rsum",
    "scatter",
    "send",
    "sticky",
    "store",
    "unpack",
    "when",
    "where",
    "xlane",
    "zero",
]
