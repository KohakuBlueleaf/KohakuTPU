"""Vector-core instruction order: a timing model of the in-order core, and a
list scheduler over it. An L1 -> L1 pass: the instructions are unchanged, only
their order is chosen.

The core (`vec_core.v`) issues in program order. What the order decides:

- ONE load/store engine. A VLD, VST, VFILL or VDRAIN starts a walk there and
  the sequencer moves on; ALU ops issue beside the walk, but the next walk
  instruction -- and anything that is neither ALU nor walk -- waits for the
  engine. Two walks back to back serialise the sequencer behind them.
- A VLD shares the register write port with ALU beats: while both run (FLAT)
  they alternate cycle by cycle. A VST reads through a spare port and does not.
- An ALU op's first beat waits until every register NAMED in its four fields
  (va, vb, vc, vd, whatever the selectors) has no write in flight, and until
  the walk is done with its register. A VLD/VST waits for its own register.
- VSHUF waits for every write-back in flight and for an idle engine.

`Timing` holds the measured constants (scripts/py/l1/primitives.py,
card_v9_1n, VL 128): an independent ALU op 11.6 cycles, a dependent one 26
after its producer, a VLD 14.4 and a VST 13.4 back to back, a VLD alternating
with an ALU op 22.4 a pair, a VSHUF 27.
"""

from dataclasses import dataclass, field

from kohakutpu.hw import vector as V
from kohakutpu.ir.l1.vector import Alu, Bar, Halt, Vdrain, Vfill, Vld, Vshuf, Vst

#: Which of va / vb / vc an ALU op reads as data (`vec_alu.v`'s operand muxes).
_UNARY = {"VMOV", "VNEG", "VABS", "VEXP2", "VLOG2", "VINV", "VRSQRT"}
_AB = {"VMUL", "VMAX", "VMIN", "VCMPLT", "VCMPGT", "VCMPEQ"}
_AC = {"VADD", "VSUB"}
_ABC = {"VFMA", "VFNMA", "VSEL"}
_CMP = {"VCMPLT", "VCMPGT", "VCMPEQ"}


@dataclass(frozen=True)
class Timing:
    """Cycles, measured on card_v9_1n at VL 128 (8 beats a register)."""

    #: Sequencer cycles an ALU op costs past its beats (11.6 - 8).
    alu_gap: float = 3.6
    #: From an ALU op's last beat to its result being readable (26 - 8).
    alu_lat: float = 18.0
    #: Issue to a walk's first word (S_EXEC, S_AGW).
    walk_setup: float = 2.0
    #: Sequencer cycles a walk instruction holds before the next issues.
    walk_issue: float = 3.0
    #: Last VLD word issued to its landing (LD_TAP).
    ld_tail: float = 4.4
    #: Last VST word to the walk's end.
    st_tail: float = 3.4
    #: VSHUF/VBCAST/VCVT: cycles a chunk (S_STR, S_STW, S_STD).
    shuf_chunk: float = 3.0
    shuf_fixed: float = 3.0
    #: VFILL issues a word address a cycle; VDRAIN moves ~1.3 cycles a word.
    fill_word: float = 1.0
    drain_word: float = 1.3
    #: S_MEMW1..S_MEM0 before a fill/drain walk starts.
    mem_setup: float = 6.0
    #: Any other instruction (VSETVL, VSETI word, ...).
    other: float = 3.0


TIMING = Timing()


def _regs_named(inst) -> set:
    """Vector registers the issue hazard checks: all four ALU fields."""
    if isinstance(inst, Alu):
        return {inst.va, inst.vb, inst.vc, inst.vd}
    if isinstance(inst, (Vld, Vst)):
        return {inst.vd if isinstance(inst, Vld) else inst.vs}
    if isinstance(inst, Vshuf):
        return {inst.va, inst.vd}
    return set()


def _reads(inst) -> set:
    """What an instruction reads as data: vector registers and predicates."""
    out: set = set()
    if isinstance(inst, Alu):
        used = (
            {"a"}
            if inst.op in _UNARY
            else (
                {"a", "b"}
                if inst.op in _AB
                else {"a", "c"} if inst.op in _AC else {"a", "b", "c"}
            )
        )
        for name, reg, sel in (
            ("a", inst.va, inst.sa),
            ("b", inst.vb, inst.sb),
            ("c", inst.vc, inst.sc),
        ):
            if name in used and sel == V.SRC_V:
                out.add(("v", reg))
        if inst.pm:
            # A predicated write keeps the lanes it does not write (vec_lanes.v
            # nx_we): it reads its destination.
            out.add(("p", inst.pr))
            if inst.op not in _CMP:
                out.add(("v", inst.vd))
    elif isinstance(inst, Vst):
        out.add(("v", inst.vs))
    elif isinstance(inst, Vshuf):
        out.add(("v", inst.va))
        if inst.pm:
            out |= {("p", inst.pr), ("v", inst.vd)}
    return out


def _writes(inst) -> set:
    if isinstance(inst, Alu):
        return {("p", inst.pr)} if inst.op in _CMP else {("v", inst.vd)}
    if isinstance(inst, (Vld, Vshuf)):
        return {("v", inst.vd)}
    return set()


@dataclass
class L1Map:
    """Where descriptor walks land in L1, so loads and stores can be ordered by
    the words they touch. `walks[ad]` is ``(base, offsets)``: a VLD/VST at
    offset `off` touches ``base + off + o`` for each `o`; `fills[ad]` the words
    a VFILL/VDRAIN through `ad` covers from its L1 offset. A descriptor not
    named is assumed to touch everything."""

    walks: dict = field(default_factory=dict)
    fills: dict = field(default_factory=dict)

    def touched(self, inst):
        if isinstance(inst, (Vld, Vst)):
            if inst.ad not in self.walks:
                return None
            base, offs = self.walks[inst.ad]
            return frozenset(base + inst.off + o for o in offs)
        if isinstance(inst, (Vfill, Vdrain)):
            if inst.ad not in self.fills:
                return None
            return frozenset(range(inst.l1, inst.l1 + self.fills[inst.ad]))
        return frozenset()

    def length(self, inst) -> int:
        n = self.fills.get(inst.ad)
        if n is None:
            raise ValueError(f"descriptor {inst.ad}'s walk length is not in the L1 map")
        return n


def _fixed(inst) -> bool:
    """Instructions nothing is reordered across."""
    return not isinstance(inst, (Alu, Vld, Vst, Vshuf, Vfill, Vdrain, Bar))


def _deps(code: list, l1: L1Map) -> list[set]:
    """``preds[i]``: the instructions `i` must follow."""
    preds = [set() for _ in code]
    last_w: dict = {}
    readers: dict = {}
    mem: list = []  # (index, words or None, writes)
    last_fixed = None
    bars: list = []
    fills: list = []
    for i, inst in enumerate(code):
        if last_fixed is not None:
            preds[i].add(last_fixed)
        if _fixed(inst):
            preds[i].update(range(last_fixed + 1 if last_fixed is not None else 0, i))
            last_fixed = i
            continue
        for r in _reads(inst):
            if r in last_w:
                preds[i].add(last_w[r])
        for r in _writes(inst):
            if r in last_w:
                preds[i].add(last_w[r])
            preds[i].update(readers.get(r, ()))
        for r in _reads(inst):
            readers.setdefault(r, []).append(i)
        for r in _writes(inst):
            last_w[r] = i
            readers[r] = []
        # L1: a store or fill against any load/store/fill/drain of the same words.
        if isinstance(inst, (Vld, Vst, Vfill, Vdrain)):
            words = l1.touched(inst)
            writes = isinstance(inst, (Vst, Vfill))
            for j, w2, wr2 in mem:
                if (writes or wr2) and (words is None or w2 is None or words & w2):
                    preds[i].add(j)
            mem.append((i, words, writes))
        # VBAR waits for EVERY fill in flight: a fill must not pass a barrier
        # in either direction, and a load must not pass the barrier before it.
        if isinstance(inst, Bar):
            preds[i].update(fills)
            preds[i].update(j for j, w, _ in mem if isinstance(code[j], Vld))
            bars.append(i)
        elif isinstance(inst, (Vfill, Vld)):
            preds[i].update(bars)
            if isinstance(inst, Vfill):
                fills.append(i)
    for i, p in enumerate(preds):
        p.discard(i)
    return preds


@dataclass
class _Walk:
    kind: type
    reg: int | None
    words_left: float
    words_from: float  # time the walk's words begin
    end: float  # when the engine is free (provisional for a VLD)


class Core:
    """The in-order issue model: feed instructions, read `t` (cycles)."""

    def __init__(self, timing: Timing = TIMING, l1: L1Map | None = None, beats=8):
        self.tm, self.l1, self.beats = timing, l1 or L1Map(), beats
        self.seq = 0.0
        self.ready: dict = {}
        self.walk: _Walk | None = None
        self.wb_until = 0.0  # every ALU write-back landed

    def _ld_words_done(self, w: _Walk) -> float:
        return w.words_from + w.words_left

    def start(self, inst) -> float:
        """When `inst` would begin issuing, without committing it."""
        s = self.seq
        named = _regs_named(inst)
        reg_ready = max((self.ready.get(r, 0.0) for r in named), default=0.0)
        w = self.walk
        if isinstance(inst, Alu):
            s = max(s, reg_ready)
            if w is not None and w.reg is not None and w.reg in named:
                s = max(s, w.end)
            return s
        engine = w.end if w is not None else 0.0
        if isinstance(inst, (Vld, Vst)):
            return max(s, engine, reg_ready)
        if isinstance(inst, Vshuf):
            return max(s, engine, self.wb_until)
        return max(s, engine)

    def issue(self, inst) -> None:
        tm, b = self.tm, self.beats
        s = self.start(inst)
        w = self.walk
        if w is not None and w.end <= s:
            self.walk = w = None
        if isinstance(inst, Alu):
            beats_end = s + b
            if w is not None and w.kind is Vld:
                # Words left at `s` share the write port with these beats, 1:1.
                left = (
                    max(0.0, w.words_from + w.words_left - s)
                    if w.words_from < s
                    else w.words_left
                )
                shared = min(left, b)
                beats_end = s + b + shared
                done = max(w.words_from, s) + left + shared
                w.end = done + tm.ld_tail
                if w.reg is not None:
                    self.ready[w.reg] = w.end
            self.seq = beats_end + tm.alu_gap
            for r in _writes(inst):
                if r[0] == "v":
                    self.ready[r[1]] = beats_end + tm.alu_lat
            self.wb_until = max(self.wb_until, beats_end + tm.alu_lat)
            return
        if isinstance(inst, Vld):
            first = s + tm.walk_setup
            self.walk = _Walk(Vld, inst.vd, b, first, first + b + tm.ld_tail)
            self.ready[inst.vd] = self.walk.end
            self.seq = s + tm.walk_issue
            return
        if isinstance(inst, Vst):
            first = s + tm.walk_setup
            self.walk = _Walk(Vst, inst.vs, b, first, first + b + tm.st_tail)
            self.seq = s + tm.walk_issue
            return
        if isinstance(inst, Vshuf):
            end = s + tm.shuf_fixed + tm.shuf_chunk * b
            self.ready[inst.vd] = end
            self.seq = end
            return
        if isinstance(inst, (Vfill, Vdrain)):
            n = self.l1.length(inst)
            per = tm.fill_word if isinstance(inst, Vfill) else tm.drain_word
            first = s + tm.mem_setup
            self.walk = _Walk(type(inst), None, n, first, first + n * per)
            self.seq = first
            return
        if isinstance(inst, Halt):
            self.seq = max(s, self.wb_until)
            return
        self.seq = s + tm.other * len(inst.words())

    @property
    def t(self) -> float:
        end = self.seq
        if self.walk is not None:
            end = max(end, self.walk.end)
        return max(end, max(self.ready.values(), default=0.0))


def cycles(code, l1: L1Map | None = None, timing: Timing = TIMING) -> float:
    """Modelled cycles of `code` run once, in its own order."""
    core = Core(timing, l1)
    for inst in code:
        core.issue(inst)
    return core.t


def _critical(code: list, preds: list) -> list[float]:
    """Longest latency path from each instruction to the end."""
    lat = []
    for inst in code:
        if isinstance(inst, Alu):
            lat.append(26.0)
        elif isinstance(inst, Vld):
            lat.append(14.4)
        elif isinstance(inst, Vst):
            lat.append(13.4)
        elif isinstance(inst, Vshuf):
            lat.append(27.0)
        else:
            lat.append(4.0)
    succs = [[] for _ in code]
    for i, p in enumerate(preds):
        for j in p:
            succs[j].append(i)
    out = [0.0] * len(code)
    for i in reversed(range(len(code))):
        out[i] = lat[i] + max((out[j] for j in succs[i]), default=0.0)
    return out


def _list(code, preds, crit, l1, timing, slack: float) -> list:
    """One greedy pass: of the instructions whose predecessors are placed, those
    able to start within `slack` cycles of the soonest compete, and the longest
    remaining path wins."""
    left = [len(p) for p in preds]
    succs = [[] for _ in code]
    for i, p in enumerate(preds):
        for j in p:
            succs[j].append(i)
    ready = {i for i in range(len(code)) if not left[i]}
    core = Core(timing, l1)
    out = []
    while ready:
        starts = {i: core.start(code[i]) for i in ready}
        soon = min(starts.values())
        best = max(
            (i for i in ready if starts[i] <= soon + slack),
            key=lambda i: (crit[i], -starts[i], -i),
        )
        ready.discard(best)
        core.issue(code[best])
        out.append(best)
        for j in succs[best]:
            left[j] -= 1
            if not left[j]:
                ready.add(j)
    if len(out) != len(code):
        raise RuntimeError("a dependence cycle: the program order is not a schedule")
    return out


#: Slack values `schedule` tries; the model keeps the best.
SLACKS = (0.0, 2.0, 4.0, 8.0, 12.0, 16.0, 24.0)


def _best_list(code, l1, timing) -> list[int]:
    preds = _deps(code, l1)
    crit = _critical(code, preds)
    best, best_t = list(range(len(code))), cycles(code, l1, timing)
    for slack in SLACKS:
        order = _list(code, preds, crit, l1, timing, slack)
        t = cycles([code[i] for i in order], l1, timing)
        if t < best_t:
            best, best_t = order, t
    return best


def schedule(code, l1: L1Map | None = None, timing: Timing = TIMING) -> list:
    """`code` reordered for the core: same instructions, same dependences.

    The short instructions are list-scheduled against `Core`, once per
    `SLACKS` value, keeping the order the model times fastest (the program's
    own order included). A VFILL or VDRAIN nothing else waits on holds the
    engine for hundreds of cycles, so it is then placed by search: every legal
    position, the fastest kept. Fixed instructions (VSETI, VSETVL, VSETMODE,
    VLOOP, VHALT) stay where they are and nothing crosses them.
    """
    code = list(code)
    l1 = l1 or L1Map()
    preds = _deps(code, l1)
    walks = {i for i, inst in enumerate(code) if isinstance(inst, (Vfill, Vdrain))}
    needed = {
        j
        for i, p in enumerate(preds)
        if not _fixed(code[i]) and i not in walks
        for j in p
    }
    long = sorted(walks - needed)
    short = [i for i in range(len(code)) if i not in long]
    order = [short[k] for k in _best_list([code[i] for i in short], l1, timing)]

    def place(i) -> float:
        pos = {j: p for p, j in enumerate(order)}
        lo = max((pos[j] + 1 for j in preds[i] if j in pos), default=0)
        hi = min(
            (pos[j] for j in pos if j > i and (_fixed(code[j]) or i in preds[j])),
            default=len(order),
        )
        best_p, best_t = lo, None
        for p in range(lo, hi + 1):
            t = cycles(
                [code[j] for j in order[:p]] + [code[i]] + [code[j] for j in order[p:]],
                l1,
                timing,
            )
            if best_t is None or t < best_t:
                best_p, best_t = p, t
        order.insert(best_p, i)
        return best_t

    for i in long:
        t = place(i)
    # Each walk was placed against those before it only: re-place each against
    # all the others until nothing moves (three rounds at most).
    for _ in range(3 if len(long) > 1 else 0):
        before = t
        for i in long:
            order.remove(i)
            t = place(i)
        if t >= before:
            break
    placed = [code[i] for i in order]
    listed = [code[i] for i in _best_list(code, l1, timing)]
    return min(placed, listed, key=lambda c: cycles(c, l1, timing))
