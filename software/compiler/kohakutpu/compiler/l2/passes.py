"""L2 -> L2 passes (docs/spec/l2-program.md §2): each narrows a choice L2
left open and prints L2 text with it written in; nothing computed changes.

| pass | decides |
|---|---|
| place | `mg[any]` / `vc[any]`: instance i on unit i mod the count |
| space | `@any` scratch: a cluster -> core transfer where the tiles allow; `@mem` / else: staging when it fits, else DRAM |
| share | a fill every cluster of a `par` makes on one view, in a body over several nodes: `share=cross` (one read a pair) |
| split | `gemm ... split` over operands larger than a bank: the K-chunk loop |
| pipeline | a node-scope `for` whose iterations run pars of different unit kinds: `pipe=2` |
| retile | a vector stream loop's rows a trip: the multiple the issue model times fastest |
"""

from dataclasses import replace
from itertools import pairwise

from kohakuaccel.compiler.machine import STAGE_BYTES
from kohakutpu.compiler import target as T
from kohakutpu.compiler.l2 import body as B
from kohakutpu.compiler.l2 import cluster
from kohakutpu.compiler.l2 import lower as L
from kohakutpu.compiler.l2 import memory as MEM
from kohakutpu.compiler.l2 import vector as V
from kohakutpu.compiler.layout.buffer import Buffer
from kohakutpu.language.text import module as M
from kohakutpu.language.text.meta import Expander
from kohakutpu.language.text.syntax import (
    Assign,
    BinOp,
    Int,
    Member,
    Name,
    Offset,
    Placed,
    Pos,
    Slice,
    Span,
    Stmt,
    TextError,
    View,
    emit,
    fmt,
    parse,
)

#: Staging a node's scratch may take: the queue holds the first megabyte.
STAGE_ROOM = STAGE_BYTES // 2


def run(
    module,
    name: str,
    passes=("place", "space", "share", "split", "pipeline", "retile"),
    machine=None,
):
    """`module` with `fn name.l2` rewritten by `passes`, in order; each
    result is read back (and so re-verified by the reader) before the next."""
    for p in passes:
        module = PASSES[p](module, name, machine or T.machine(nodes=4))
    return module


def _body(module, name):
    src = module.body(name, "l2")
    r = module.reader()
    macros = {m.name: (m.params, m.stmts) for m in module.macros.values()}
    return src, Expander(macros, r.fail).stmts(src.stmts, {}), r


def _write(module, name, src, stmts) -> object:
    params = ", ".join(f"{p}: {fmt(t)}" for p, t in src.params)
    head = f"target {module.target}\n\n" if module.target else ""
    body = emit(stmts, 1)
    text = f"{head}fn {name}.l2({params})\n{body}"
    return M.read(text, module.file)


def _walk(stmts, f):
    """`stmts` with `f(stmt, scopes)` applied to every statement, depth first;
    `scopes` the enclosing par / for statements."""

    def go(sts, scopes):
        out = []
        for s in sts:
            s = replace(s, body=go(s.body, [*scopes, s]))
            got = f(s, scopes)
            out += got if isinstance(got, list) else [got]
        return out

    return go(stmts, [])


# ------------------------------------------------------------------ place
def place(module, name, machine):
    src, stmts, r = _body(module, name)

    def f(s, scopes):
        if s.op != "par":
            return s
        pos = s.positional()
        unit = pos[2] if len(pos) == 3 else None
        if not isinstance(unit, View) or unit.slices != (Name("any"),):
            return s
        kind = {"mg": "MG", "vc": "VC"}.get(unit.name)
        if kind is None:
            r.fail(s, f"{unit.name}[any] is not a unit kind")
        count = machine.count(kind)
        var, dom = pos[0].lhs, pos[0].rhs
        idx = var if dom.lo == Int(0) else BinOp("-", var, dom.lo)
        n = (
            (dom.hi.value - dom.lo.value)
            if isinstance(dom.hi, Int) and isinstance(dom.lo, Int)
            else None
        )
        if n is None or n > count:
            idx = BinOp("%", idx, Int(count))
        args = [*s.args[:2], View(unit.name, (idx,))]
        return replace(s, args=args)

    return _write(module, name, src, _walk(stmts, f))


# ------------------------------------------------------------------ space
def _space(s) -> str | None:
    if s.op != "buffer" or not isinstance(s.annot, Placed):
        return None
    sp = s.annot.space
    return sp.name if hasattr(sp, "name") else sp.text


def space(module, name, machine):
    """`@any`: a scratch tile the clusters of one `par` drain and only the
    cores of the next read back, a band a trip, becomes a transfer straight
    into those cores' L1 (`drain -> vc[..]`, `recv`) when every core reads
    exactly one cluster's tile and it fits the landing ring; else, like
    `@mem`, staging when it fits, else DRAM."""
    src, stmts, _ = _body(module, name)
    for s in [s for s in stmts if _space(s) == "any"]:
        got = _direct(module, name, src, stmts, s.target)
        if got is not None:
            stmts = [t for t in got if t.target != s.target or t.op != "buffer"]
    out = []
    for s in stmts:
        if _space(s) in ("mem", "any"):
            sp = s.annot.space
            b = Buffer.of(Placed(s.annot.x, Name("dram")))
            to = "stage" if b.nbytes <= STAGE_ROOM else "dram"
            new = Name(to) if isinstance(sp, Name) else replace(sp, name=to)
            s = replace(s, annot=Placed(s.annot.x, new))
        out.append(s)
    return _write(module, name, src, out)


def _direct(module, name, src, stmts, h):
    """`stmts` with scratch `h` turned into cluster -> core transfers, or
    None where the shapes do not allow it (see `space`)."""
    as_dram = [_to_dram(s) if s.op == "buffer" and s.target == h else s for s in stmts]
    pair = _drain_load_pair(as_dram, h)
    if pair is None:
        return None
    p1, p2, drain, load = pair
    try:
        body = B.read(_write(module, name, src, as_dram), name)
    except TextError:
        return None
    tile = _match(body, h)
    if tile is None:
        return None
    var1 = p1.positional()[0].lhs.text
    var2 = p2.positional()[0].lhs.text
    unit1, unit2 = p1.positional()[2].slices[0], p2.positional()[2].slices[0]
    lo1 = drain.positional()[2].slices[0].lo or Int(0)
    name_l1 = f"{h}_l1"

    def f(s, scopes):
        if s.op == "drain" and _views(s.positional()[2]) == h:
            to = View("vc", (_subst(unit2, {var2: Name(var1)}),))
            return replace(s, args=[*s.args[:2], to, *s.args[3:]])
        if s.op != "par" or _unit(s) != "vc":
            return s
        loop = next((b for b in s.body if b.op == "for"), None)
        ld = next(
            (
                b
                for b in (loop.body if loop else [])
                if b.op == "load" and b.line == load.line
            ),
            None,
        )
        if ld is None:
            return s
        rows = ld.positional()[0].slices[0]
        rel = BinOp("-", rows.lo, _subst(lo1, {var1: Name(var2)}))
        band = View(name_l1, (Slice(rel, rows.hi), Slice()))
        body = []
        for b in loop.body:
            if b is ld:
                continue
            if b.op == "unpack" and b.positional() == [Name(ld.target)]:
                b = replace(b, args=[band])
            body.append(b)
        recv = Stmt(
            "recv",
            [View("mg", (_subst(unit1, {var1: Name(var2)}),))],
            name_l1,
            Placed(View("f16", tuple(Int(d) for d in tile)), _gt4_l1()),
            line=ld.line,
        )
        rest = [replace(loop, body=body) if b is loop else b for b in s.body]
        return replace(s, body=[recv, *rest])

    return _walk(stmts, f)


def _to_dram(s):
    sp = s.annot.space
    return replace(
        s,
        annot=Placed(
            s.annot.x,
            Name("dram") if isinstance(sp, Name) else replace(sp, name="dram"),
        ),
    )


def _gt4_l1():
    (st,) = parse("x = y : f16[4, 4] @l1(gt4)")
    return st.annot.space


def _mentions(s, h) -> bool:
    """Whether a statement's arguments name buffer `h` (a view or a name)."""

    def go(x) -> bool:
        if isinstance(x, View):
            return x.name == h or any(go(y) for y in x.slices)
        if isinstance(x, Name):
            return x.text == h
        if isinstance(x, (tuple, list)):
            return any(go(y) for y in x)
        if hasattr(x, "__dataclass_fields__") and not isinstance(x, Stmt):
            return any(go(getattr(x, k)) for k in x.__dataclass_fields__)
        return False

    return go(s.args)


def _views(t):
    """The buffer a view (or a bare name) reads."""
    if isinstance(t, View):
        return t.name
    return t.text if isinstance(t, Name) else None


def _subst(t, names: dict):
    """`t` with each name in `names` replaced by its term."""
    if isinstance(t, Name):
        return names.get(t.text, t)
    if isinstance(t, (tuple, list)):
        return type(t)(_subst(x, names) for x in t)
    if hasattr(t, "__dataclass_fields__") and not isinstance(t, (Int, Stmt)):
        return replace(
            t, **{k: _subst(getattr(t, k), names) for k in t.__dataclass_fields__}
        )
    return t


def _drain_load_pair(stmts, h):
    """``(cluster par, core par, the drain, the load)`` when `h` is drained
    by one statement of one cluster `par`, loaded by one statement of the
    stream loop of the vector `par` right after it (in the same block), and
    touched by nothing else; else None."""
    found = {"drain": [], "load": [], "other": 0}

    def scan(sts, scopes):
        for i, s in enumerate(sts):
            if s.op == "par" and _unit(s) == "mg":
                nxt = sts[i + 1] if i + 1 < len(sts) else None
                found.setdefault("next", {})[id(s)] = nxt
            reads = _mentions(s, h)
            if s.op == "drain" and reads:
                found["drain"].append((s, scopes[-1] if scopes else None))
            elif s.op == "load" and reads:
                found["load"].append((s, scopes))
            elif reads:
                found["other"] += 1
            scan(s.body, [*scopes, s])

    scan(stmts, [])
    if found["other"] or len(found["drain"]) != 1 or len(found["load"]) != 1:
        return None
    drain, p1 = found["drain"][0]
    load, scopes = found["load"][0]
    if p1 is None or p1.op != "par" or _unit(p1) != "mg" or len(scopes) < 2:
        return None
    loop, p2 = scopes[-1], scopes[-2]
    if loop.op != "for" or p2.op != "par" or _unit(p2) != "vc":
        return None
    if found.get("next", {}).get(id(p1)) is not p2:
        return None
    return p1, p2, drain, load


def _match(body, h):
    """The tile's ``(rows, cols)`` when, in every stage pair, the cluster
    instance with each `par` value drains one tile of `h` and the core
    instance with that value loads exactly its row bands in order, a trip a
    band of whole tile rows; the tile fits a core's landing ring. Else None."""
    stages = [x for k, _, x, _ in B.instances(body) if k == "par"]
    tile, pairs = None, 0
    for first, second in pairwise(stages):
        drains = _drained(body, first, h)
        if not drains:
            continue
        loads = _loaded(body, second, h)
        if loads is None or set(loads) != set(drains):
            return None
        for v, (region, shape) in drains.items():
            if loads[v] != region:
                return None
            if tile not in (None, shape):
                return None
            tile = shape
        pairs += 1
    if not pairs or tile is None or tile[0] * tile[1] // 16 > L.POOL:
        return None
    return tile


def _drained(body, insts, h) -> dict:
    """``{par value: ((row lo, rows, col lo, cols), (rows, cols))}`` of the
    cluster instances that drain into `h`."""
    out = {}
    for i in insts:
        if i.kind != "mg":
            return {}
        for op, env in i.items:
            if op.op == "drain" and _views(op.st.positional()[2]) == h:
                v = B.value(body, op.st, op.st.positional()[2], env)
                ax = _axes(body, v)
                if ax is None:
                    return {}
                (r0, r, _), (c0, c, _) = ax
                out[env[i.par.var]] = ((r0, r, c0, c), (r, c))
    return out


def _loaded(body, insts, h) -> dict | None:
    """``{par value: (row lo, rows, col lo, cols)}`` the core instances load
    from `h` over their stream loops, each trip's band right after the last;
    None if some load is not such a band."""
    out = {}
    for i in insts:
        if i.kind != "vc":
            return None
        loop = next((s for s in i.par.body if isinstance(s, B.For)), None)
        if loop is None:
            return None
        ld = next(
            (s for s in loop.body if isinstance(s, B.Op) and s.op == "load"), None
        )
        if ld is None:
            return None
        bands = []
        for t in B.span(body, loop, i.env):
            v = B.value(body, ld.st, ld.st.positional()[0], {**i.env, loop.var: t})
            ax = _axes(body, v)
            if ax is None:
                return None
            bands.append(ax)
        (r0, r, _), (c0, c, _) = bands[0]
        for k, ((rk, rr, _), (ck, cc, _)) in enumerate(bands):
            if (rk, rr, ck, cc) != (r0 + k * r, r, c0, c):
                return None
        out[i.env[i.par.var]] = (r0, r * len(bands), c0, c)
    return out


def _axes(body, v):
    buf = body.buffers.get(v.name) if isinstance(v, View) else None
    if buf is None or len(buf.shape) != 2:
        return None
    try:
        return MEM.axes(v, buf.shape)
    except MEM.ViewError:
        return None


# ------------------------------------------------------------------ share
def share(module, name, machine):
    src, stmts, _ = _body(module, name)
    nodes = any(s.op == "par" and _unit(s) == "node" for s in stmts)
    if not nodes:
        return module

    def f(s, scopes):
        if s.op != "fill" or "share" in s.kwargs():
            return s
        pars = [p for p in scopes if p.op == "par" and _unit(p) == "mg"]
        if not pars:
            return s
        var = pars[-1].positional()[0].lhs.text
        view = s.positional()[0]
        if var in _names(view):
            return s
        return replace(s, args=[*s.args, Assign(Name("share"), Name("cross"))])

    return _write(module, name, src, _walk(stmts, f))


def _unit(s) -> str | None:
    pos = s.positional()
    return pos[2].name if len(pos) == 3 and isinstance(pos[2], View) else None


def _names(t) -> set:
    out = set()

    def go(x):
        if isinstance(x, Name):
            out.add(x.text)
        elif isinstance(x, (tuple, list)):
            for y in x:
                go(y)
        elif hasattr(x, "__dataclass_fields__"):
            for k in x.__dataclass_fields__:
                go(getattr(x, k))

    go(t)
    return out


# ------------------------------------------------------------------ split
def split(module, name, machine):
    """`a = fill A_VIEW : mx7a[M, K] @A`, `b = fill B_VIEW : mx7b[N, K] @B`,
    `c = gemm a, b[, c] split : ...` with a bank too small for M or N at K:
    the three become a loop over K in chunks of the operands' packing."""
    src, stmts, r = _body(module, name)
    params = dict(src.params)

    def f(s, scopes):
        if not s.body:
            return s
        body, i = list(s.body), 0
        out = []
        while i < len(body):
            st = body[i]
            if st.op == "gemm" and Name("split") in st.positional():
                if len(out) < 2 or out[-2].op != "fill" or out[-1].op != "fill":
                    r.fail(st, "gemm ... split follows the two fills it reads")
                fa, fb = out.pop(-2), out.pop(-1)
                out += _split(r, st, fa, fb, params, scopes, body)
            else:
                out.append(st)
            i += 1
        return replace(s, body=out)

    return _write(module, name, src, _walk(stmts, f))


def _split(r, gemm, fa, fb, params, scopes, siblings) -> list:
    m_t, n_t = fa.annot.x, fb.annot.x
    rows_a, k = (d.value for d in m_t.slices)
    rows_b = n_t.slices[0].value
    view_a, view_b = fa.positional()[0], fb.positional()[0]
    buf = Buffer.of(params[view_a.name])
    chunk = buf.layout["nk"] * 32
    if k % chunk or k == chunk:
        r.fail(gemm, f"split: K = {k} is not several packed chunks of {chunk}")
    if max(rows_a, rows_b) // 4 * (chunk // 32) > cluster.BANK:
        r.fail(gemm, "split: one K chunk of an operand still overflows a bank")
    used = {p.positional()[0].lhs.text for p in scopes if p.op in ("for", "par")}
    var = next(v for v in ("kk", "k2", "k3", "k4") if v not in used)
    kv = Name(var)

    def chunked(view):
        *lead, ks = view.slices
        lo = ks.lo if ks.lo is not None else Int(0)
        return View(
            view.name,
            (*lead, Slice(Offset(lo, BinOp("*", kv, Int(chunk))), Pos(Int(chunk)))),
        )

    def typed(t):
        x = t.x
        return Placed(View(x.name, (x.slices[0], Int(chunk))), t.space)

    a = replace(fa, args=[chunked(view_a), *fa.args[1:]], annot=typed(fa.annot))
    b = replace(fb, args=[chunked(view_b), *fb.args[1:]], annot=typed(fb.annot))
    names = [x for x in gemm.positional() if x != Name("split")]
    acc = gemm.target
    first = []
    if len(names) == 2:
        first = [Stmt("zero", [], acc, gemm.annot, line=gemm.line)]
        names = [*names, Name(acc)]
    g = replace(gemm, args=names)
    loop = Stmt(
        "for",
        [Member(kv, Span(Int(0), Int(k // chunk)))],
        body=[a, b, g],
        line=gemm.line,
    )
    return [*first, loop]


# --------------------------------------------------------------- pipeline
#: Iterations a pipelined node loop keeps in flight: a core's landing ring
#: holds two tiles of up to half its size.
PIPE = 2


def pipeline(module, name, machine):
    """`for` at node scope (outside any unit's `par`) whose body runs pars of
    two unit kinds: the node may send the next iteration ahead (`pipe=2`)."""
    src, stmts, _ = _body(module, name)

    def kinds(s) -> set:
        out = set()
        for b in s.body:
            if b.op == "par" and _unit(b) not in (None, "node"):
                out.add(_unit(b))
            elif b.op in ("for", "when"):
                out |= kinds(b)
        return out

    def f(s, scopes):
        unit = any(p.op == "par" and _unit(p) != "node" for p in scopes)
        if s.op != "for" or unit or "pipe" in s.kwargs() or len(kinds(s)) < 2:
            return s
        return replace(s, args=[*s.args, Assign(Name("pipe"), Int(PIPE))])

    return _write(module, name, src, _walk(stmts, f))


# ----------------------------------------------------------------- retile
#: Rows a trip the retile pass tries, as multiples of the written tile; the
#: written tile first, the one the others must beat.
RETILE = (1, 0.25, 0.5, 2, 4)


def retile(module, name, machine):
    """Each vector stream loop `for t in 0..T` of a vector `par` whose views
    all step `t*R` rows with `R` rows a trip: the multiple m of R (T
    divisible) whose lowering the issue model times fastest over all its
    trips, 2% better or the written tile. Rows, views and types scale by m;
    the loop runs T / m trips. A `par` inside node-scope loops is costed at
    their first iteration."""
    src, stmts, _ = _body(module, name)
    top = [s for s in stmts if s.op == "buffer"]

    def f(s, scopes):
        if s.op != "par" or _unit(s) != "vc" or not all(map(_scope, scopes)):
            return s
        loops = [b for b in s.body if b.op == "for"]
        if len(loops) != 1:
            return s
        loop = loops[0]
        rows = _stream_rows(loop)
        var, dom = loop.positional()[0].lhs.text, loop.positional()[0].rhs
        if rows is None or not isinstance(dom.hi, Int) or not isinstance(dom.lo, Int):
            return s
        trips = dom.hi.value - dom.lo.value
        best, cost = s, None
        for m in RETILE:
            new_rows = rows * m
            if new_rows != int(new_rows) or new_rows % 4 or (trips * rows) % new_rows:
                continue
            new = loop if m == 1 else _scaled(loop, var, rows, int(new_rows), dom)
            cand = replace(s, body=[new if b is loop else b for b in s.body])
            got = _vector_cost(module, name, src, [*top, _first(scopes, cand)])
            if got is not None and (cost is None or got < cost * 0.98):
                best, cost = cand, got
        return best

    return _write(module, name, src, _walk(stmts, f))


def _scope(s) -> bool:
    """A statement a vector `par` may sit in for the retile pass to cost it:
    a node-scope `for`, a `par` over nodes, a `when`."""
    if s.op == "when":
        return True
    if s.op == "for" or (s.op == "par" and _unit(s) == "node"):
        dom = s.positional()[0].rhs
        return isinstance(dom.lo, Int)
    return False


def _first(scopes, inner):
    """`inner` inside `scopes` (outermost first), each loop narrowed to its
    first iteration."""
    for s in reversed(scopes):
        if s.op in ("for", "par"):
            head = s.positional()[0]
            lo = head.rhs.lo
            one = Member(head.lhs, Span(lo, Int(lo.value + 1)))
            kw = [a for a in s.args[1:] if not isinstance(a, Assign)]
            s = replace(s, args=[one, *kw])
        inner = replace(s, body=[inner])
    return inner


def _stream_rows(loop) -> int | None:
    """R when every op of the loop has a leading dimension of R (or a
    statistic of R rows) and every memory view steps `VAR*R`: else None."""
    rows = set()
    for st in loop.body:
        if st.body:
            return None
        if isinstance(st.annot, Placed):
            dims = st.annot.x.slices
            if dims and isinstance(dims[0], Int):
                rows.add(dims[0].value)
    rows.discard(1)
    if len(rows) != 1:
        return None
    return rows.pop()


def _scaled(loop, var: str, rows: int, new: int, dom) -> object:
    """`loop` with R rows a trip made `new`: `VAR*R` -> `VAR*new`, `+R` ->
    `+new` in row slices, shapes R -> new, the trips scaled to match."""

    def term(t):
        if isinstance(t, BinOp) and t.op == "*" and Name(var) in (t.a, t.b):
            k = t.b if t.a == Name(var) else t.a
            if isinstance(k, Int) and k.value == rows:
                return BinOp("*", Name(var), Int(new))
        if isinstance(t, Pos) and t.x == Int(rows):
            return Pos(Int(new))
        if isinstance(t, BinOp):
            return BinOp(t.op, term(t.a), term(t.b))
        if isinstance(t, Offset):
            return Offset(term(t.base), term(t.off))
        if isinstance(t, Slice):
            return Slice(
                term(t.lo) if t.lo is not None else None,
                term(t.hi) if t.hi is not None else None,
            )
        if isinstance(t, View):
            return View(t.name, tuple(term(x) for x in t.slices))
        return t

    def shape(p):
        x = p.x
        dims = list(x.slices)
        if dims and dims[0] == Int(rows):
            dims[0] = Int(new)
        return Placed(View(x.name, tuple(dims)), p.space)

    body = []
    for st in loop.body:
        args = [term(a) if not isinstance(a, Assign) else a for a in st.args]
        annot = shape(st.annot) if isinstance(st.annot, Placed) else st.annot
        body.append(replace(st, args=args, annot=annot))
    trips = (dom.hi.value - dom.lo.value) * rows // new
    span = Span(dom.lo, Int(dom.lo.value + trips))
    head = Member(Name(var), span)
    return replace(loop, args=[head, *loop.args[1:]], body=body)


def _vector_cost(module, name, src, stmts):
    """The issue model's cycles over every trip for the first instance of
    the one vector `par` in `stmts`, or None where it does not lower."""
    try:
        body = B.read(_write(module, name, src, stmts), name)
        stage = next(x for k, _, x, _ in B.instances(body) if k == "par")
        return V.total(body, stage[0].par, stage[0].env, body.reader.fail)
    except (TextError, ValueError, StopIteration):
        return None


PASSES = {
    "place": place,
    "space": space,
    "share": share,
    "split": split,
    "pipeline": pipeline,
    "retile": retile,
}

__all__ = ["PASSES", "run"]
