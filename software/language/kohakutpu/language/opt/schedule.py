"""L1 -> L1: list scheduling of each image's math.

The vector core's math queue issues in order, so an op waiting on a source
stalls every op behind it. Each run of consecutive math statements (any other
statement ends a run and keeps its place) is reordered over a unit model from
the target: `ceil(vl / lanes)` beats an op, results readable
`vector_latency` cycles after the last beat, one cycle for a config word when
an op's VL / selects / crossbar differ from the op before. Dependences are per
register chunk and predicate; `xor`, `rot`, `merge` read the whole register.
Images still holding `expand` / `for` / `when` are left as written (known only
once expanded with arguments).
"""

from kohakutpu.language.l1.ops import COMPARE, MATH, READS_B, READS_C
from kohakutpu.language.text.module import read as read_module
from kohakutpu.language.text.syntax import (
    Call,
    Int,
    Name,
    Slice,
    View,
    emit,
    fmt,
    parse,
)

META = {"expand", "for", "when"}


def _int(t):
    return t.value if isinstance(t, Int) else None


def _chunks(t, n: int, chunks: int) -> set:
    """The chunks a register term `t` (`vN`, `vN[c:]`, `vN[c]`) covers over
    `n` beats."""
    if isinstance(t, View) and len(t.slices) == 1:
        (s,) = t.slices
        if isinstance(s, Int):
            return {s.value % chunks}
        if isinstance(s, Slice) and _int(s.lo) is not None:
            return {(s.lo.value + i) % chunks for i in range(n)}
    return {i % chunks for i in range(n)}


def _reg(t):
    name = t.name if isinstance(t, View) else t.text if isinstance(t, Name) else None
    if name and name[0] == "v" and name[1:].isdigit():
        return int(name[1:])
    return None


def _source(t, n: int, chunks: int) -> set:
    if isinstance(t, Call):
        inner = t.args[0] if t.args else None
        reg = _reg(inner)
        if reg is None:
            return set()
        if t.name == "bcast":
            return {("v", reg, c) for c in _chunks(inner, n, chunks)}
        return {("v", reg, c) for c in range(chunks)}
    reg = _reg(t)
    if reg is None:
        return set()
    return {("v", reg, c) for c in _chunks(t, n, chunks)}


class Op:
    """One math statement's beats, configuration and register accesses."""

    def __init__(self, st, target) -> None:
        self.st = st
        chunks = target.chunks
        pos = st.positional()
        kw = st.kwargs()
        vl = _int(kw.get("vl", Int(target.lanes * chunks)))
        self.beats = -(-vl // target.lanes) if vl else chunks
        n = self.beats
        nsrc = 1 + (st.op in READS_B) + (st.op in READS_C)
        srcs = pos[1 : 1 + nsrc]
        self.reads: set = set()
        for t in srcs:
            self.reads |= _source(t, n, chunks)
        for k in ("if", "unless"):
            if k in kw:
                self.reads.add(("p", kw[k].text))
        d = pos[0]
        if st.op in COMPARE:
            self.writes = {("p", d.text if isinstance(d, Name) else d.name)}
        else:
            reg = _reg(d)
            self.writes = {("v", reg, c) for c in _chunks(d, n, chunks)}
        self.key = (
            kw.get("vl"),
            tuple(t.slices if isinstance(t, View) else None for t in pos),
            tuple(
                (
                    (
                        t.name,
                        t.args[1:],
                        _chunks(t.args[0], 1, chunks) if t.args else None,
                    )
                    if isinstance(t, Call)
                    else None
                )
                for t in srcs
            ),
            kw.get("if"),
            kw.get("unless"),
        )


def order(stmts: list, target, latency: int | None = None) -> list:
    """`stmts` (all math) in the scheduled order."""
    return plan(stmts, target, latency)[0]


def plan(stmts: list, target, latency: int | None = None) -> tuple:
    """`stmts` (all math) in the scheduled order, and the model's cycles
    from the first issue to the last result; `latency` defaults to the
    target's `vector_latency`."""
    lat_r = target.vector_latency if latency is None else latency
    ops = [Op(st, target) for st in stmts]
    n = len(ops)
    preds: list = [dict() for _ in range(n)]
    last_w: dict = {}
    readers: dict = {}
    for j, o in enumerate(ops):
        for r in o.reads:
            i = last_w.get(r)
            if i is not None:
                preds[j][i] = max(preds[j].get(i, 0), ops[i].beats + lat_r)
        for w in o.writes:
            i = last_w.get(w)
            if i is not None:
                preds[j].setdefault(i, 1)
            for i in readers.get(w, ()):
                if i != j:
                    preds[j].setdefault(i, 1)
        for r in o.reads:
            readers.setdefault(r, []).append(j)
        for w in o.writes:
            last_w[w] = j
            readers[w] = []
    succs: list = [[] for _ in range(n)]
    for j in range(n):
        for i, lat in preds[j].items():
            succs[i].append((j, lat))
    height = [0] * n
    for i in reversed(range(n)):
        height[i] = max([lat + height[j] for j, lat in succs[i]] + [ops[i].beats])
    waiting = [len(p) for p in preds]
    ready = [i for i in range(n) if not waiting[i]]
    start = [None] * n
    out = []
    free, key = 0, None
    while ready:
        est = {i: _earliest(ops[i], preds[i], start, free, key) for i in ready}
        best = min(ready, key=lambda i: (est[i], -height[i], i))
        start[best] = est[best]
        free, key = start[best] + ops[best].beats, ops[best].key
        ready.remove(best)
        out.append(stmts[best])
        for j, _ in succs[best]:
            waiting[j] -= 1
            if not waiting[j]:
                ready.append(j)
    if len(out) != n:
        raise AssertionError("the dependence graph has a cycle")
    span = max((start[i] + ops[i].beats + lat_r for i in range(n)), default=0)
    return out, span


def _earliest(op: Op, preds: dict, start: list, free: int, key) -> int:
    """The first cycle `op` can issue: the unit free (and its config word),
    every predecessor's result or order met."""
    t = free + (op.key != key)
    for p, lat in preds.items():
        t = max(t, start[p] + lat)
    return t


def cost(stmts: list, target) -> int:
    """Model cycles of a statement list's math runs, as they stand (loops
    once, no other statement timed)."""
    total, run = 0, []
    for st in stmts + [None]:
        if st is not None and st.op in MATH and not st.body:
            run.append(st)
            continue
        if run:
            total += plan(run, target)[1]
            run = []
        if st is not None and st.body:
            total += cost(st.body, target)
    return total


def estimate(text: str, name: str, target) -> int:
    """Model cycles of `name.l1`: per core, each RUN's loop body cost times
    its first argument (the trip count), plus the code around the loop; the
    busiest core's sum."""
    m = read_module(text, "<estimate>")
    images = {}
    for k, d in m.images.items():
        loop = [st for st in d.stmts if st.op == "loop"]
        rest = [st for st in d.stmts if st.op != "loop"]
        images[k] = (cost(loop[0].body, target) if loop else 0, cost(rest, target))
    per_core: dict = {}
    for st in m.body(name, "l1").stmts:
        sends = [st] if st.op == "send" else [b for b in st.body if b.op == "send"]
        for s in sends:
            for r in s.body:
                if r.op != "run":
                    continue
                call = r.positional()[0]
                body, rest = images[call.name]
                trips = _int(call.args[0]) or 1
                core = fmt(s.positional()[0])
                per_core[core] = per_core.get(core, 0) + body * trips + rest
    return max(per_core.values(), default=0)


def block(stmts: list, target, latency: int | None = None) -> list:
    """A statement list with every math run scheduled, blocks recursively."""
    out, run = [], []
    for st in stmts:
        if st.op in MATH and not st.body:
            run.append(st)
            continue
        out += order(run, target, latency) if len(run) > 1 else run
        run = []
        if st.body:
            st.body = block(st.body, target, latency)
        out.append(st)
    out += order(run, target, latency) if len(run) > 1 else run
    return out


def _meta(stmts) -> bool:
    return any(st.op in META or _meta(st.body) for st in stmts)


def schedule(text: str, target, name: str = "<l1>", latency: int | None = None) -> str:
    """The module text with every expanded image's math scheduled."""
    stmts = parse(text, name)
    for st in stmts:
        if st.op == "image" and not _meta(st.body):
            st.body = block(st.body, target, latency)
    return emit(stmts)


__all__ = ["block", "cost", "estimate", "order", "plan", "schedule"]
