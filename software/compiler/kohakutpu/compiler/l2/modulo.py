"""Software pipelining of a vector core's stream loop (modulo scheduling).

The band lowering (`vector.py`) runs one trip's work inside one L1 tile: the
register file is that tile's scratch, so a trip's dependent chains (a merge
tree is seven of them) stall the in-order math engine. Here a trip's ops --
the same symbolic code the band lowering generates for one trip -- become an
ITERATION scheduled every II cycles, several in flight: iteration j's stage s
runs beside iteration j+1's stage s-1. Registers rotate over K copies of the
iteration (modulo variable expansion) and are coloured within the core's
register count; each load and store is a ring of K L1 slots; every VSYNC's
slack is the exact count of the instructions it may leave outstanding.

The model is the V2 core's (replay bench, vector-core-v2.md s2): one math beat
a cycle, a dependent math op `LAT_MM` after its producer's last beat; an
unpack or pack occupies its engine a word a cycle; on a two-copy register
file a beat reading three distinct registers in one bank is held a cycle.

`plan(...)` picks II, K and the registers; `emit(...)` writes the loop's
lines (the prologue, a VLOOP of K instances, the tail) and its modelled
cycles. An iteration's fills issue a lead ahead of its first unpack and its
drains after its last pack, as ops of the iteration, so every instance with
all stages present repeats the loop body. The image's IMEM words, not the
registers, bound the depth: a math op with its configuration word is about
two words, so the caller searches II upward for the first image that fits.
"""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass, field

from kohakutpu.language.l1.ops import COMPARE, READS_B, READS_C

#: math -> math: a dependent op starts this long after its producer's last beat
LAT_MM = 20
#: and this much later where it reads the value through the crossbar
#: (bcast, merge, ...; replay bench: 28 cycles after a one-beat producer)
LAT_XB = 7
#: unpack -> math, after the unpack's last word
LAT_UM = 12
#: math -> pack
LAT_MP = 20
#: a value a pack reads keeps its register this much longer than its last
#: read, when the budget allows (a pack read on a two-copy file can wait)
PAD_MIN = 64
#: the loop counter's scalar register
LOOP_S = 15
#: a fill issues this many cycles, plus LEAD_WORD a word, before its first
#: unpack (the NoC gives a vector core about half a word a cycle)
LEAD_FIXED = 64
LEAD_WORD = 2


def lead(words: int) -> int:
    """Cycles an iteration's fills of `words` L1 words issue ahead of its
    first unpack."""
    return LEAD_FIXED + LEAD_WORD * words


#: the predicate register P1 a compare writes and a predicated op reads, as a
#: dependence name (the generator's symbols are "%N")
PRED = "#p1"
#: predicate registers an iteration's copy may take (p1..p3)
PREDS = 3


def pred_copies(k: int) -> int:
    """Predicate registers the K copies rotate over: the most, up to PREDS,
    that divides K (copy j uses p(1 + j mod that))."""
    return max(d for d in range(1, PREDS + 1) if k % d == 0)


_SYM = re.compile(r"\{(\d+)\}")
_L1 = re.compile(r"l1\[(\d+)\]")


@dataclass
class MOp:
    """One instruction of an iteration."""

    tmpl: str
    syms: tuple
    eng: str  # "U" unpack, "M" math, "P" pack
    reads: tuple
    writes: tuple
    occ: int  # cycles of its engine
    l1: int | None = None  # the L1 word an unpack reads / a pack writes
    t: int = 0
    e: int = 0
    deps: list = field(default_factory=list)
    #: the reads that go through the crossbar (LAT_XB later)
    xreads: tuple = ()


def _operands(rest: str) -> list:
    """Top-level comma-separated operands of a math op."""
    out, depth, cur = [], 0, ""
    for ch in rest:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _walk(opnd: str, syms: tuple):
    """``(symbol, base chunk, stride)`` an operand reads, None for S/K or a
    predicate. A crossbar's input is its register's."""
    m = _SYM.search(opnd)
    if m is None:
        return None
    sym = syms[int(m[1])]
    sel = re.search(r"\}\[(\d+)\s*(:?)\s*\]", opnd)
    if sel is None:
        return sym, 0, 1
    return sym, int(sel[1]), 1 if sel[2] else 0


def _hold_beats(op: str, opnds: list, syms: tuple, beats: int) -> int:
    """Beats a two-copy register file holds: a, b and c all vector, three
    distinct {register, chunk} words of one bank (bank = chunk bit 0)."""
    fields = ["a"] + (["b"] if op in READS_B else []) + (["c"] if op in READS_C else [])
    srcs = dict(zip(fields, opnds[1:], strict=False))
    if set(srcs) != {"a", "b", "c"}:
        return 0
    if "(" in srcs["c"]:  # c through the crossbar is b's input
        return 0
    walks = [_walk(srcs[f], syms) for f in "abc"]
    if any(w is None for w in walks):
        return 0
    held = 0
    for i in range(beats):
        words = {(s, (b + st * i) % 8) for s, b, st in walks}
        banks = {c % 2 for _, c in words}
        if len(words) == 3 and len(banks) == 1:
            held += 1
    return held


def parse(tmpl: str, syms: tuple, copies: int) -> MOp:
    op, _, rest = tmpl.partition(" ")
    if op == "vunpk":
        n = re.search(r"\bn=(\d+)", tmpl)
        cb = re.search(r"\{0\}\[(\d+)\s*:\s*\]", tmpl)
        chunks = int(n[1]) if n else 8 - (int(cb[1]) if cb else 0)
        words = chunks * (2 if " f32" in tmpl else 1)
        partial = chunks < 8
        return MOp(
            tmpl,
            syms,
            "U",
            (syms[0],) if partial else (),
            (syms[0],),
            words,
            int(_L1.search(tmpl)[1]),
        )
    if op == "vpack":
        n = re.search(r"\bn=(\d+)", tmpl)
        words = 16 if " mx7" in tmpl else int(n[1]) if n else 8
        if " f32" in tmpl:
            words *= 2
        return MOp(tmpl, syms, "P", (syms[0],), (), words, int(_L1.search(tmpl)[1]))
    body = re.sub(r"\s+(vl|if|unless)=\S+", "", rest)
    opnds = _operands(body)
    vl = re.search(r"\bvl=(\d+)", tmpl)
    beats = max(1, -(-int(vl[1]) // 16)) if vl else 8
    dest = opnds[0] if opnds else ""
    reads = {syms[int(i)] for i in _SYM.findall(",".join(opnds[1:]))}
    xreads = {syms[int(i)] for x in opnds[1:] if "(" in x for i in _SYM.findall(x)}
    writes = ()
    if op in COMPARE:
        writes = (PRED,)
    elif _SYM.search(dest):
        # A chunk-selected or predicated result writes its words in place
        # (the core waits on no earlier math write): no read of the rest.
        writes = (syms[int(_SYM.search(dest)[1])],)
    if re.search(r"\b(if|unless)=p", tmpl):
        reads.add(PRED)
    hold = _hold_beats(op, opnds, syms, beats) if copies == 2 else 0
    return MOp(
        tmpl,
        syms,
        "M",
        tuple(sorted(reads)),
        writes,
        beats + hold,
        xreads=tuple(sorted(xreads)),
    )


def _link(ops: list) -> None:
    """RAW with latency after the producer's last beat; WAR / WAW in order."""
    last_w: dict = {}
    readers: dict = {}
    for i, o in enumerate(ops):
        for x in o.reads:
            for p in last_w.get(x, []):
                po = ops[p]
                lat = LAT_UM if po.eng == "U" else LAT_MP if o.eng == "P" else LAT_MM
                if po.eng == "M" and x in o.xreads:
                    lat += LAT_XB
                o.deps.append((p, lat, True))
        for x in o.writes:
            for p in last_w.get(x, []):
                o.deps.append((p, 1, False))
            for p in readers.get(x, []):
                o.deps.append((p, 1, False))
        for x in o.reads:
            readers.setdefault(x, []).append(i)
        for x in o.writes:
            last_w.setdefault(x, []).append(i)
            readers[x] = []


#: placement attempts an op may take in `_place`, beyond its first
PLACE_BUDGET = 6


def _place(ops: list, ii: int) -> bool:
    """Iterative modulo scheduling (Rau): ops by height (the longest
    latency path to the iteration's end) take the first run of `occ`
    consecutive free slots from their earliest start -- an engine runs an
    op's beats back to back, so the issue order is the slots' start order.
    Where no run is free an op takes its slot anyway and evicts what holds
    it (and any successor it now comes too late for), which go back in the
    queue; False when the attempts run out."""
    n = len(ops)
    if any(o.occ > ii for o in ops):
        return False
    pred = [
        [(p, lat + (ops[p].occ if end else 0)) for p, lat, end in o.deps] for o in ops
    ]
    succ: list = [[] for _ in ops]
    for i in range(n):
        for p, d in pred[i]:
            succ[p].append((i, d))
    height = [0] * n
    for i in reversed(range(n)):  # a dependence points back in program order
        height[i] = max((d + height[s] for s, d in succ[i]), default=ops[i].occ)
    owner = {e: [None] * ii for e in "MUP"}
    at: list = [None] * n
    last: list = [None] * n
    queue = [(-height[i], i) for i in range(n)]
    heapq.heapify(queue)
    budget = (PLACE_BUDGET + 1) * n

    def evict(j: int) -> None:
        row = owner[ops[j].eng]
        for b in range(ops[j].occ):
            row[(at[j] + b) % ii] = None
        at[j] = None
        heapq.heappush(queue, (-height[j], j))

    while queue:
        _, i = heapq.heappop(queue)
        if at[i] is not None:
            continue
        budget -= 1
        if budget < 0:
            return False
        o, row = ops[i], owner[ops[i].eng]
        early = max((at[p] + d for p, d in pred[i] if at[p] is not None), default=0)
        lo = early if last[i] is None or last[i] < early else last[i] + 1
        for t in range(lo, lo + ii):
            if all(row[(t + b) % ii] is None for b in range(o.occ)):
                break
        else:
            t = lo
            for j in {row[(t + b) % ii] for b in range(o.occ)} - {None}:
                evict(j)
        for b in range(o.occ):
            row[(t + b) % ii] = i
        at[i] = last[i] = t
        for s, d in succ[i]:
            if at[s] is not None and at[s] < t + d:
                evict(s)
    for i, o in enumerate(ops):
        o.t, o.e = at[i], at[i] + o.occ
    return True


def _lifetimes(ops: list, pinned: set) -> dict:
    life: dict = {}
    for o in ops:
        for x in o.writes:
            if x in pinned or x == PRED:
                continue
            s, e = life.get(x, (o.t, o.t))
            life[x] = (min(s, o.t), max(e, o.e))
    for o in ops:
        for x in o.reads:
            if x in pinned or x == PRED:
                continue
            s, e = life.get(x, (o.t, o.t))
            life[x] = (s, max(e, o.e + (4 if o.eng == "P" else 0)))
    return life


def _colour(life: dict, ii: int, k: int, regs: int, first: int) -> dict | None:
    """Registers for (symbol, copy j): arcs on a circle of K x II, lowest
    free register from `first`."""
    period = ii * k
    arcs = []
    for x, (s, e) in life.items():
        if e - s > period:
            return None
        for j in range(k):
            arcs.append((s + j * ii, e + j * ii, x, j))
    arcs.sort()
    busy: dict = {}
    out = {}

    def overlap(a, b) -> bool:
        return any(a[0] < b[1] + sh and b[0] + sh < a[1] for sh in (-period, 0, period))

    for s, e, x, j in arcs:
        for r in range(first, regs):
            if all(not overlap((s, e), iv) for iv in busy.get(r, [])):
                busy.setdefault(r, []).append((s, e))
                out[(x, j)] = r
                break
        else:
            return None
    return out


@dataclass
class Plan:
    ops: list
    ii: int
    k: int
    stages: int
    phys: dict
    #: the iteration's fills (every load ring's) and drains (every store
    #: ring's), as times of the iteration; None without loads / stores
    fill_t: int | None = None
    drain_t: int | None = None
    #: the earliest stage of the iteration (a fill may run before stage 0)
    s0: int = 0

    def stage(self, t: int) -> int:
        return t // self.ii - self.s0


def _padded(life: dict, packed: set, n: int) -> dict:
    """`life` with every value a pack reads kept `n` cycles longer."""
    return {x: (s, e + (n if x in packed else 0)) for x, (s, e) in life.items()}


def plan(
    ops: list,
    regs: int,
    pinned: dict,
    k_max: int,
    ii_max: int,
    lead: int,
    ii_min: int = 1,
) -> Plan | None:
    """The smallest II from `ii_min` whose placement fits and whose registers
    colour within `regs` (pinned symbols on their own registers), at the
    smallest K that gives packed values PAD_MIN cycles of reuse slack (else
    the most slack). The iteration's fills issue `lead` cycles before its
    first unpack, its drains at its last pack's end; K covers both rings'
    reuse. None past `ii_max` or `k_max`."""
    first = max(pinned.values(), default=-1) + 1
    lower = max(sum(o.occ for o in ops if o.eng == e) for e in ("M", "U", "P"))
    for ii in range(max(lower, ii_min, 1), ii_max + 1):
        for o in ops:
            o.deps, o.t, o.e = [], 0, 0
        _link(ops)
        if not _place(ops, ii):
            continue
        life = _lifetimes(ops, set(pinned))
        us = [o for o in ops if o.eng == "U"]
        ps = [o for o in ops if o.eng == "P"]
        fill_t = min(o.t for o in us) - lead if us else None
        drain_t = max(o.e for o in ps) if ps else None
        times = [o.t for o in ops] + [t for t in (fill_t, drain_t) if t is not None]
        s0 = min(times) // ii
        stages = max(times) // ii - s0 + 1
        k_min = 2
        if us:
            # a fill reuses the slot of the iteration K back, after its unpacks
            k_min = max(k_min, -(-(max(o.e for o in us) - fill_t + 1) // ii))
        if ps:
            # a pack reuses the slot of the iteration K back, after its drain
            k_min = max(k_min, (drain_t - min(o.t for o in ps)) // ii + 1)
        packed = {x for o in ops if o.eng == "P" for x in o.reads if x in life}
        writers = [o.t for o in ops if PRED in o.writes]
        readers = [o.e for o in ops if PRED in o.reads]
        p_span = max(readers, default=0) - min(writers, default=0)
        best = None
        for k in range(k_min, k_max + 1):
            if writers and p_span >= pred_copies(k) * ii:
                continue
            phys = _colour(life, ii, k, regs, first)
            if phys is None:
                continue
            lo, hi = 0, ii * k
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _colour(_padded(life, packed, mid), ii, k, regs, first) is not None:
                    lo = mid
                else:
                    hi = mid - 1
            if lo:
                phys = _colour(_padded(life, packed, lo), ii, k, regs, first)
            got = Plan(ops, ii, k, stages, phys, fill_t, drain_t, s0)
            if lo >= PAD_MIN:
                return got
            if best is None or lo > best[0]:
                best = (lo, got)
        if best is not None:
            return best[1]
    return None


@dataclass
class Ring:
    """A load or store's K L1 slots: `base` the trip-0 buffer the generated
    code names, `slots` each copy's first word."""

    ad: int
    kind: str  # "load" | "store"
    base: int
    words: int
    slots: tuple


#: How the stream's last iterations finish: "explicit", an epilogue of the
#: stages left over the real iterations only; "kernel", the loop body runs on
#: through them, the iterations past the stream filling, unpacking and
#: computing but never draining (a drain is an iteration's last op), so a
#: load ring reads up to (stages + K) iterations' rows past its input's end.
TAILS = ("explicit", "kernel")
#: an iteration's fills and drains among its ops in `emit`
_FILL, _DRAIN = -1, -2
#: `emit` ends each op's line with its modelled issue cycle (`# m=N`, the
#: loop body's for its first repetition) and opens with `# modulo II K`
ANNOTATE = False


def emit(
    p: Plan, rings: list, trips: int, pinned: dict, tail: str = "explicit"
) -> tuple:
    """``(lines, modelled cycles)``: the prologue, a VLOOP over K instances,
    the rest (`tail`). Instance n holds stage s of iteration n - s; an
    iteration's fills and drains are its own ops, each ring slot's reuse a
    sync on the iteration K back. ``(None, 0)`` where the loop body is not
    periodic or a sync would precede its producer."""
    ii, k, s_n = p.ii, p.k, p.stages
    ops = p.ops
    items = [(o.t, i) for i, o in enumerate(ops)]
    if p.fill_t is not None:
        items.append((p.fill_t, _FILL))
    if p.drain_t is not None:
        items.append((p.drain_t, _DRAIN))
    order = sorted(items, key=lambda x: (x[0] % ii, x[0]))
    loads = [r for r in rings if r.kind == "load"]
    stores = [r for r in rings if r.kind == "store"]
    us = [i for i, o in enumerate(ops) if o.eng == "U"]
    ps = [i for i, o in enumerate(ops) if o.eng == "P"]
    first_u = min(us, key=lambda i: ops[i].t, default=None)
    last_u = max(us, key=lambda i: ops[i].t, default=None)
    first_p = min(ps, key=lambda i: ops[i].t, default=None)
    last_p = max(ps, key=lambda i: ops[i].t, default=None)

    def ring_of(word: int):
        for r in rings:
            if r.base <= word < r.base + r.words:
                return r
        raise ValueError(f"l1[{word}] is in no ring")

    def text(i: int, j: int) -> str:
        o = ops[i]
        names = []
        for s in o.syms:
            names.append(f"v{pinned[s]}" if s in pinned else f"v{p.phys[(s, j % k)]}")
        line = o.tmpl.format(*names)
        if PRED in o.reads or PRED in o.writes:
            line = re.sub(r"\bp1\b", f"p{1 + j % k % pred_copies(k)}", line)
        if o.l1 is not None:
            r = ring_of(o.l1)
            at = r.slots[j % k] + (o.l1 - r.base)
            line = _L1.sub(f"l1[{at}]", line, count=1)
        return line

    # The whole emission as events, then each sync's exact slack.
    seq: list = []  # ("i", text, kind or None) | ("s", waiter, on, need)
    count = {"fill": 0, "unpack": 0, "pack": 0, "drain": 0}
    done: dict = {kind: {} for kind in count}  # iteration -> count after its last

    def insn(t: str, kind: str | None = None) -> None:
        if kind:
            count[kind] += 1
        seq.append(("i", t, kind))

    def note(n: int, t: int) -> str:
        return f"  # m={n * ii + t % ii}" if ANNOTATE else ""

    def wait(waiter: str, on: str, j: int) -> bool:
        """A sync on iteration j's `on` events (none before iteration 0);
        False where they are not yet in the program."""
        if j >= 0 and j not in done[on]:
            return False
        seq.append(("s", waiter, on, done[on][j] if j >= 0 else None))
        return True

    n_inst = trips + s_n - 1
    lo_loop = s_n - 1
    reps = max(0, (n_inst if tail == "kernel" else trips) - lo_loop) // k
    hi_loop = lo_loop + reps * k
    marks = []  # seq index at each instance's start
    for n in range(n_inst):
        marks.append(len(seq))
        past = tail == "kernel" and lo_loop <= n < hi_loop
        for t, i in order:
            j = n - p.stage(t)
            if j < 0 or (j >= trips and not past):
                continue
            if i == _FILL:
                if not wait("fill", "unpack", j - k):
                    return None, 0
                for r in loads:
                    insn(f"vfill a{r.ad} -> l1[{r.slots[j % k]}] rel", "fill")
                done["fill"][j] = count["fill"]
            elif i == _DRAIN:
                if j >= trips or not wait("drain", "pack", j):
                    return None, 0
                for r in stores:
                    insn(f"vdrain a{r.ad} <- l1[{r.slots[j % k]}] rel", "drain")
                done["drain"][j] = count["drain"]
            elif ops[i].eng == "U":
                if (
                    i == first_u
                    and p.fill_t is not None
                    and not wait("unpack", "fill", j)
                ):
                    return None, 0
                insn(text(i, j) + note(n, t), "unpack")
                if i == last_u:
                    done["unpack"][j] = count["unpack"]
            elif ops[i].eng == "P":
                if (
                    i == first_p
                    and p.drain_t is not None
                    and not wait("pack", "drain", j - k)
                ):
                    return None, 0
                insn(text(i, j) + note(n, t), "pack")
                if i == last_p:
                    done["pack"][j] = count["pack"]
            else:
                insn(text(i, j) + note(n, t))
    marks.append(len(seq))
    run = {"fill": 0, "unpack": 0, "pack": 0, "drain": 0}
    exact = []
    for ev in seq:
        if ev[0] == "i":
            if ev[2]:
                run[ev[2]] += 1
            exact.append(None)
        else:
            _, _, on, need = ev
            exact.append(63 if need is None else max(0, min(63, run[on] - need)))
    lines: list = [f"# modulo {ii} {k}"] if ANNOTATE else []

    def put(lo: int, hi: int, slack_of) -> None:
        for q in range(lo, hi):
            ev = seq[q]
            if ev[0] == "i":
                lines.append(ev[1])
            else:
                lines.append(f"vsync {ev[1]} on={ev[2]} slack={slack_of(q)}")

    put(0, marks[0], lambda q: exact[q])
    put(marks[0], marks[lo_loop], lambda q: exact[q])
    if reps:
        a, b = marks[lo_loop], marks[lo_loop + k]
        per = b - a
        for rep in range(reps):
            a2 = marks[lo_loop + rep * k]
            for q in range(per):
                x, y = seq[a + q], seq[a2 + q]
                same = x[0] == y[0] and (
                    x[1].split("  #")[0] == y[1].split("  #")[0]
                    if x[0] == "i"
                    else x[1:3] == y[1:3]
                )
                if not same:
                    return None, 0
        slack = []
        for q in range(per):
            vals = [exact[marks[lo_loop + rep * k] + q] for rep in range(reps)]
            vals = [v for v in vals if v is not None]
            slack.append(min(vals) if vals else None)
        lines.append(f"seti s{LOOP_S} = {reps}")
        lines.append(f"loop s{LOOP_S}")
        lines.append("{")
        put(a, b, lambda q: slack[q - a])
        lines.append("}")
    put(marks[hi_loop], marks[n_inst], lambda q: exact[q])
    return lines, ii * n_inst


__all__ = ["MOp", "Plan", "Ring", "emit", "parse", "plan"]
