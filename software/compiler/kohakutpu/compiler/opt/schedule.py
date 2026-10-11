"""L1 -> L1: list scheduling of vector-core images.

Each run of consecutive math, unpack and pack instructions in an image is
reordered, dependences kept, to start each instruction as early as the V2
issue model (`kohakutpu.compiler.l2.vector.estimate`'s) allows; every other
statement (a sync, a fill or drain, a scalar op, a loop) stays where it is
and bounds the runs on either side. Configuration words are the encoder's
(it tracks the core's state), so a reorder never changes what an op reads.

Dependences: on vector registers (a chunk select counts as the whole
register), scalar and predicate registers, and the L1 words an unpack reads
or a pack writes.
"""

from dataclasses import replace

from kohakutpu.language.text import module as M
from kohakutpu.language.text.meta import Expander
from kohakutpu.language.text.syntax import (
    Call,
    Int,
    Name,
    Slice,
    Stmt,
    View,
    emit,
    fmt,
)

MATH_LATENCY = 20
QUEUE = 8
WALK = 8
MOVABLE = {"vunpk", "vpack"}


def schedule_module(module) -> object:
    """`module` with every image's runs list-scheduled; the result is read
    back. Images are expanded first (macros, `for`, `when`)."""
    r = module.reader()
    macros = {m.name: (m.params, m.stmts) for m in module.macros.values()}
    texts = [f"target {module.target}\n"] if module.target else []
    for img in module.images.values():
        body = Expander(macros, r.fail, unroll_all=True).stmts(img.stmts, {})
        head = f"image {img.name}({', '.join(img.params)})\n"
        texts.append(head + emit(_block(body), 1))
    for m in module.macros.values():
        texts.append(f"macro {m.name}({', '.join(m.params)})\n" + emit(m.stmts, 1))
    for (name, level), b in module.fns.items():
        params = ", ".join(f"{p}: {fmt(t)}" for p, t in b.params)
        texts.append(f"fn {name}.{level}({params})\n" + emit(b.stmts, 1))
    return M.read("\n".join(texts), module.file)


def _block(stmts: list) -> list:
    out, run = [], []
    for st in stmts:
        if _movable(st):
            run.append(st)
            continue
        out += schedule(run)
        run = []
        out.append(replace(st, body=_block(st.body)) if st.body else st)
    return out + schedule(run)


def _movable(st: Stmt) -> bool:
    if st.target is not None or st.body:
        return False
    return st.op in MOVABLE or (st.op.startswith("v") and st.op not in _FIXED)


#: Image statements that are not math and stay in place.
_FIXED = {
    "vfill",
    "vdrain",
    "vsync",
    "vwait",
    "vpost",
    "vmark",
    "vbar",
    "vsetvl",
    "vcfg",
    "vseti",
}


# ---------------------------------------------------------------- the model
def _regs(t, out: set) -> set:
    """Every register name a term reads or writes."""
    if isinstance(t, Name):
        if t.text[:1] in "vskp" and t.text[1:].isdigit():
            out.add(t.text)
    elif isinstance(t, View):
        _regs(Name(t.name), out)
    elif isinstance(t, Call):
        for a in t.args:
            _regs(a, out)
    return out


def _l1(st: Stmt):
    """The L1 words an unpack reads or a pack writes, or None."""
    for a in st.positional():
        if isinstance(a, View) and a.name == "l1" and a.slices:
            s = a.slices[0]
            off = s.value if isinstance(s, Int) else None
            if off is None and isinstance(s, Slice) and isinstance(s.lo, Int):
                off = s.lo.value
            if off is None:
                return None
            n = st.kwargs().get("n")
            words = n.value if isinstance(n, Int) else 8
            return set(range(off, off + words))
    return None


def _info(st: Stmt) -> tuple:
    """``(engine, defs, uses, l1 words, l1 write?)``."""
    pos = st.positional()
    if st.op == "vunpk":
        return "u", _regs(pos[0], set()), set(), _l1(st), False
    if st.op == "vpack":
        return "p", set(), _regs(pos[0], set()), _l1(st), True
    defs = _regs(pos[0], set()) if pos else set()
    uses = set()
    for a in pos[1:]:
        _regs(a, uses)
    partial = bool(pos) and isinstance(pos[0], View)
    if partial or st.op.startswith("vcmp"):
        uses |= defs
    for k, v in st.kwargs().items():
        if k in ("if", "unless"):
            _regs(v, uses)
    return "m", defs, uses, None, False


def _vl(st: Stmt) -> int:
    v = st.kwargs().get("vl")
    return v.value if isinstance(v, Int) else 128


#: Dispatch cycles a configuration word costs (CHKVL or XB the encoder
#: inserts when an op's VL, chunk selects or crossbar differ from the core's).
CFG_COST = 4
#: A reordered run is kept only when the model times it this much faster.
MIN_GAIN = 0.01


def _cfg(st: Stmt) -> tuple:
    """``(vl and chunk selects, crossbar)`` a math op needs; None for the
    unpack / pack engines."""
    if st.op in MOVABLE:
        return None
    chunks, xbar = [], None
    for a in st.positional():
        if isinstance(a, View):
            chunks.append(a.slices)
        elif isinstance(a, Call):
            xbar = (a.name, tuple(a.args[1:]))
            inner = a.args[0] if a.args else None
            if isinstance(inner, View):
                chunks.append(inner.slices)
        else:
            chunks.append(None)
    return ((_vl(st), tuple(chunks)), xbar)


def cost(run: list) -> int:
    """The issue model's cycles for `run` in its written order."""
    return _simulate(run, list(range(len(run))))


def _simulate(run, order) -> int:
    info = [_info(st) for st in run]
    cfgs = [_cfg(st) for st in run]
    state = {"chk": None, "xb": None}
    ready_at: dict = {}
    eng = {"u": 0, "m": 0, "p": 0}
    starts = {"u": [], "m": [], "p": []}
    wport = dispatch = 0
    for k in order:
        e, d, u, _, _ = info[k]
        q = starts[e]
        dispatch = max(dispatch + 1, q[-QUEUE] if len(q) >= QUEUE else 0)
        if cfgs[k] is not None:
            chk, xb = cfgs[k]
            dispatch += CFG_COST * ((chk != state["chk"]) + (xb != state["xb"]))
            state["chk"], state["xb"] = chk, xb
        s = max(dispatch, eng[e], *(ready_at.get(r, 0) for r in u))
        if e in ("u", "m"):
            s = max(s, wport)
        if e == "u":
            eng[e] = wport = s + WALK
            for r in d:
                ready_at[r] = eng[e]
        elif e == "p":
            eng[e] = s + WALK
        else:
            eng[e] = wport = s + max(1, _vl(run[k]) // 16)
            for r in d:
                ready_at[r] = s + MATH_LATENCY
        q.append(s)
    return max(eng.values())


def schedule(run: list) -> list:
    """`run` reordered: repeatedly the ready instruction that starts
    earliest under the issue model, a configuration change counted (ties
    keep the written order). The written order stands unless the model
    times the new one `MIN_GAIN` faster."""
    if len(run) < 3:
        return run
    info = [_info(st) for st in run]
    cfgs = [_cfg(st) for st in run]
    n = len(run)
    preds = [set() for _ in range(n)]
    for j in range(n):
        ej, dj, uj, wj, wrj = info[j]
        for i in range(j):
            ei, di, ui, wi, wri = info[i]
            reg = di & uj or di & dj or ui & dj
            l1 = wi is not None and wj is not None and wi & wj and (wri or wrj)
            whole = (wi is None or wj is None) and ei != "m" and ej != "m"
            if reg or l1 or whole:
                preds[j].add(i)
    done, order = set(), []
    ready_at: dict = {}
    eng_done = {"u": 0, "m": 0, "p": 0}
    starts = {"u": [], "m": [], "p": []}
    state = {"chk": None, "xb": None}
    wport = 0
    dispatch = 0

    def start_of(k):
        e, _, u, _, _ = info[k]
        q = starts[e]
        disp = max(dispatch + 1, q[-QUEUE] if len(q) >= QUEUE else 0)
        if cfgs[k] is not None:
            chk, xb = cfgs[k]
            disp += CFG_COST * ((chk != state["chk"]) + (xb != state["xb"]))
        s = max(disp, eng_done[e])
        if e in ("u", "m"):
            s = max(s, wport)
        s = max([s, *(ready_at.get(r, 0) for r in u)])
        return s, disp

    while len(order) < n:
        cands = [k for k in range(n) if k not in done and preds[k] <= done]
        best = min(cands, key=lambda k: (start_of(k)[0], k))
        s, dispatch = start_of(best)
        e, d, _, _, _ = info[best]
        if cfgs[best] is not None:
            state["chk"], state["xb"] = cfgs[best]
        if e == "u":
            eng_done[e] = wport = s + WALK
            for r in d:
                ready_at[r] = eng_done[e]
        elif e == "p":
            eng_done[e] = s + WALK
        else:
            eng_done[e] = wport = s + max(1, _vl(run[best]) // 16)
            for r in d:
                ready_at[r] = s + MATH_LATENCY
        starts[e].append(s)
        done.add(best)
        order.append(best)
    if _simulate(run, order) > cost(run) * (1 - MIN_GAIN):
        return run
    return [run[k] for k in order]


__all__ = ["schedule", "schedule_module"]
