"""A V2 vector core on the abstract machine (`vec_cu.v`, `v2_core.v`,
docs/projects/kohakutpu/vector-core-v2.md s2 and s4).

The CU takes IMEM and DESC words between RUNs and starts the core at a RUN;
the RUN completes at VHALT. The core is six processes: the front end
dispatches in program order into five engine queues (FILL 4, DRAIN 4, UNPACK
8, MATH 8, PACK 8) and stalls on a full one; each engine runs its queue in
order, an entry once the older register accesses it conflicts with have
finished (s2), a sync entry once the instructions it names have (s4), a
`vwait` once its counter reached its target. Counters: granules landed by
COUNT bursts (RX) and credits received (CR), both kept across RUNs.
"""

import functools

from kohakutpu.compiler.encode import vector as A
from kohakutpu.compiler.isa import core as C
from kohakutpu.compiler.isa.vector import ISA as VEC
from kohakutpu.compiler.verify.memory import Memory
from kohakutpu.compiler.verify.model import Clock, Port, Proc

L1_WORDS = 512
#: the most words one peer drain may carry (`v2_core.v` F_LEN)
PEER_WORDS = 256
DEPTH = {"F": 4, "D": 4, "U": 8, "M": 8, "P": 8}
#: VSYNC waiter -> the queue it rides (0: the front end itself)
WAITER = {1: "U", 2: "M", 3: "P", 4: "F", 6: "D"}
#: VSYNC kind -> the instructions it counts (DUALQ = 0: queue 1 is queue 0)
KIND = {0: "F", 1: "U", 2: "M", 3: "P", 4: "D", 5: "U", 6: "P"}
MATH = {op.upper() for op in C.OPS}
UNARY = {"VMOV", "VNEG", "VABS", "VEXP2", "VLOG2", "VINV", "VRSQRT"}
READS_AC = {"VADD", "VSUB"}
READS_AB = {"VMUL", "VMAX", "VMIN", "VEXP2D", "VCMPLT", "VCMPGT", "VCMPEQ"}
QUEUE = {"VUNPK": "U", "VPACK": "P", "VFILL": "F", "VDRAIN": "D", "VPOST": "D", "VBAR": "U"}
#: the instruction kind an op counts as for VSYNC / marks
OWN_KIND = {"VUNPK": "U", "VPACK": "P", "VFILL": "F", "VDRAIN": "D"}


@functools.lru_cache(maxsize=1 << 16)
def _decode(word: int) -> tuple:
    fmt = C.CORE.find(word)
    return (None, None) if fmt is None else (fmt, fmt.decode(word))


class Vector:
    kind = "VC"

    def __init__(self, card, key: tuple, name: str) -> None:
        self.card, self.key, self.name = card, key, name
        m = card.m
        self.words: list = []
        self.rx = m.counter(f"rx:{name}")
        self.cr = m.counter(f"cr:{name}")
        #: what earlier `vwait`s on each counter asked for (kept across RUNs)
        self.target = {C.RX: 0, C.CR: 0}
        self.done = m.counter(f"done:{name}")
        self.recv = m.counter(f"recv:{name}")
        self.acks = m.counter(f"acks:{name}")
        self.imem: dict = {}
        self.desc: dict = {}
        self.ainc: dict = {}  # descriptor -> its running offset's increment
        #: walk configuration, kept across RUNs (`v2_core.v` resets it only
        #: with the core): pointers, increments, (s0, s1) strides
        self.cfg = {"UPTR": 0, "UINC": 0, "PPTR": 0, "PINC": 0, "USTR": (1, 4), "PSTR": (1, 4)}
        self.port = Port(m, name)
        self.mem = Memory(m, name)
        self.cu = _Cu(self)
        #: the process whose events are this unit's completions
        self.completer = self.cu.ix
        self.fe = _Front(self)
        self.engines = {k: _Engine(self, k) for k in DEPTH}
        #: bursts landed here: (sender proc, send clock, landing clock)
        self.landings: list = []

    def deliver(self, word: int, clock) -> None:
        self.words.append((word, clock))

    def land(self, buf: int, off: int, n: int, count: bool, clock, what: str, sent=None) -> str:
        """`n` granules at word `off` of the one L1 (`vec_cu.v`: buffer 0
        only, rejected past L1_WORDS). Returns why it is rejected, or ""."""
        if buf != 0:
            return f"buffer {buf}: a vector core has only buffer 0"
        if off + n - 1 >= L1_WORDS:
            return f"words {off}..{off + n - 1} past the {L1_WORDS}-word L1"
        self.mem.access("L1", range(off, off + n), True, clock, self.port.ix, what)
        if count:
            self.rx.add(n, clock, self.port.ix)
        return ""

    def joins(self, buf: int) -> list:
        return []

    def credit(self, n: int, clock, owner: int) -> None:
        self.cr.add(n, clock, owner)

    def rest(self) -> list:
        out = []
        if self.rx.value != self.target[C.RX]:
            out.append(f"{self.rx.value - self.target[C.RX]} granules landed and never waited for")
        if self.cr.value != self.target[C.CR]:
            out.append(f"{self.cr.value - self.target[C.CR]} credits received and never waited for")
        return out

    def walk_words(self, ad: int) -> int:
        n = 1
        for fld in range(1, 5):
            bound = self.desc.get((ad, fld), 0) & 0xFFFF
            n *= max(bound, 1)
        return n


class _Cu(Proc):
    """The CU: a word at a time while the core is idle."""

    def __init__(self, u: Vector) -> None:
        super().__init__(u.card.m)
        self.u = u
        self.name = f"{u.name} CU"
        self.pc = 0
        self.running = False

    def done(self) -> bool:
        return self.pc >= len(self.u.words) and not self.running and self.u.card.node_done(self.u)

    def blocked(self) -> str:
        if self.running:
            return "its RUN has not halted"
        return "waiting for words from the node"

    def step(self) -> bool:
        u = self.u
        if self.running:
            if not u.fe.halted:
                return False
            self.running = False
            clock = self.tick(u.fe.clock)
            u.done.add(1, clock, self.ix)
            u.recv.add(1, clock, self.ix)
            return True
        if self.pc >= len(u.words):
            return False
        word, given = u.words[self.pc]
        fmt = VEC.set.find(word)
        self.pc += 1
        clock = self.tick(given)
        if fmt is None:
            u.card.m.find("model", self.name, "a word decodes as no vector CU instruction")
        else:
            f = fmt.decode(word)
            if fmt.name == "IMEM":
                u.imem[VEC.imem_addr(f)] = f["word"]
            elif fmt.name == "DESC":
                hi = f["value_hi"] << VEC.cfg.desc_value_bits
                u.desc[(f["ad"], f["fld"])] = f["value"] | hi
            else:
                u.fe.start(f["pc"], clock)
                self.running = True
                return True
        u.done.add(1, clock, self.ix)
        u.recv.add(1, clock, self.ix)
        return True


class _Entry:
    __slots__ = ("op", "f", "deps", "guard", "clock", "done", "roff", "l1")

    def __init__(self, op, f, clock) -> None:
        self.op, self.f, self.clock = op, f, clock
        self.deps: list = []
        self.guard = None
        self.done = None  # its clock once finished
        self.roff = 0  # a `rel` transfer's running offset (AINC)
        self.l1 = None  # (L1 words, writes, what) it touches


#: L1 words a walk of `chunks` chunks takes, by mode (vector-core-v2.md s6)
def _walk_words(mode: int, chunks: int, pack: bool) -> int:
    if pack and mode == A.MX7:
        return 4
    return 2 * chunks if mode == A.F32 else chunks


class _Front(Proc):
    """Fetch, decode, scalars, loops, dispatch."""

    def __init__(self, u: Vector) -> None:
        super().__init__(u.card.m)
        self.u = u
        self.name = f"{u.name} front end"
        self.halted = True
        self.pc = 0
        self.why = ""
        #: per kind: instructions dispatched, and the clock of each finished
        self.count = {k: 0 for k in DEPTH}
        self.finished = {k: [] for k in DEPTH}

    def start(self, pc: int, clock) -> None:
        self.halted = False
        self.pc = pc
        self.s: dict = {}
        self.loop = None  # (first pc, end pc, trips left)
        self.marks: dict = {}
        self.roff: dict = {}  # descriptor -> running offset, from 0 each RUN
        self.regs: dict = {}  # register -> [(entry, access)] not yet finished
        self.pending = None  # a front-end wait's guard, fixed when first met
        self.tick(clock)

    def done(self) -> bool:
        return self.halted

    def blocked(self) -> str:
        return f"pc {self.pc}: {self.why}"

    def fetch(self, pc: int) -> int:
        w = self.u.imem.get(pc)
        if w is None:
            self.u.card.m.find("model", self.name, f"pc {pc} holds no IMEM word")
            return C.HALT.encode()
        return w

    def advance(self, n: int = 1) -> None:
        self.pc += n
        if self.loop is not None and self.pc >= self.loop[1]:
            first, end, left = self.loop
            if left > 1:
                self.loop = (first, end, left - 1)
                self.pc = first
            else:
                self.loop = None

    def finished_upto(self, kind: str, upto: int):
        """``(whether the first `upto` instructions of `kind` finished, the
        clock of the last)``: an engine finishes its queue in order."""
        done = self.finished[kind]
        if upto <= 0:
            return True, []
        if len(done) < upto:
            return False, []
        return True, [done[upto - 1]]

    def step(self) -> bool:
        if self.halted:
            return False
        u = self.u
        word = self.fetch(self.pc)
        fmt, f = _decode(word)
        if fmt is None:
            u.card.m.find("model", self.name, f"pc {self.pc}: no core instruction")
            self.halted = True
            return False
        name = fmt.name
        if name == "VHALT":
            busy = [k for k, e in u.engines.items() if e.head < len(e.items)]
            if busy:
                self.why = f"halt: engines {''.join(busy)} still busy"
                return False
            self.tick(*(e.clock for e in u.engines.values()))
            self.halted = True
            return True
        if name == "VSETI":
            imm = self.fetch(self.pc + 1) & 0xFFFFFF
            if not f["to_k"]:
                self.s[f["sreg"]] = imm
            self.tick()
            self.advance(2)
            return True
        if name == "VLOOP":
            trips = self.s.get(f["sreg"])
            if not isinstance(trips, int):
                u.card.m.find("model", self.name, f"pc {self.pc}: a loop over a count not set by VSETI")
                self.halted = True
                return False
            self.tick()
            if trips == 0:
                self.advance(1 + f["body"])
            else:
                self.loop = (self.pc + 1, self.pc + 1 + f["body"], trips)
                self.pc += 1
            return True
        if name == "VSKIPZ":
            s = self.s.get(f["sreg"], "unknown")
            self.tick()
            if not isinstance(s, int):
                # Data-dependent: the block runs (the constrained path, which
                # may touch no counter: verify.md lemma 4).
                self._check_block(self.pc + 1, f["skip"])
                self.advance(1)
            elif s == 0:
                self.advance(1 + f["skip"])
            else:
                self.advance(1)
            return True
        if name == "GETSTK":
            self.s[f["sreg"]] = "unknown"
            self.tick()
            self.advance()
            return True
        if name == "VMARK":
            kind = KIND[f["on"]]
            self.marks[f["mark"]] = (kind, max(self.count[kind] - f["slack"], 0))
            self.tick()
            self.advance()
            return True
        if name in ("VSYNC", "VWAIT") and f["waiter"] == 0:
            if self.pending is None:
                self.pending = self._guard(name, f)
            ok, rel = self.pending()
            if not ok:
                self.why = f"{name.lower()} on the front end"
                return False
            self.pending = None
            self.tick(*rel)
            self.advance()
            return True
        queue = "M" if name in MATH else QUEUE.get(name)
        if queue is None and name in ("VSYNC", "VWAIT"):
            queue = WAITER.get(f["waiter"])
            if queue is None:
                u.card.m.find("model", self.name, f"pc {self.pc}: waiter {f['waiter']}")
                self.halted = True
                return False
        if queue is None:
            if name == "AINC":
                u.ainc[f["ad"]] = f["inc"]
            elif name in ("UPTR", "UINC", "PPTR", "PINC"):
                u.cfg[name] = f["ptr"] & (L1_WORDS - 1)
            elif name in ("USTR", "PSTR"):
                u.cfg[name] = (f["s0"], f["s1"])
            self.tick()  # front-end state: VCFG, VSETVL, AINC, ...
            self.advance()
            return True
        eng = u.engines[queue]
        if len(eng.items) - eng.head >= DEPTH[queue]:
            self.why = f"queue {queue} full"
            return False
        e = _Entry(name, f, self.tick())
        if name in ("VFILL", "VDRAIN") and f["rel"]:
            e.roff = self.roff.get(f["ad"], 0)
            self.roff[f["ad"]] = e.roff + u.ainc.get(f["ad"], 0)
        e.l1 = self._footprint(name, f)
        self._deps(e)
        if name in ("VSYNC", "VWAIT", "VBAR"):
            e.guard = self._guard(name, f)
        kind = "M" if name in MATH else OWN_KIND.get(name)
        if kind is not None:
            self.count[kind] += 1
            eng.kinds.append(kind)
        else:
            eng.kinds.append(None)
        eng.items.append(e)
        self.advance()
        return True

    def _footprint(self, name: str, f: dict):
        """The L1 words an instruction reads or writes, fixed at dispatch:
        a walk's word w at base + (w mod 4)*s0 + (w div 4)*s1, the base its
        offset (plus the pointer, which then advances, under `rel`); a fill
        or drain the walk's length from its L1 offset."""
        u = self.u
        what = f"{name} at pc {self.pc}"
        if name in ("VUNPK", "VPACK"):
            p = "U" if name == "VUNPK" else "P"
            base = f["off"]
            if f["rel"]:
                base += u.cfg[f"{p}PTR"]
                u.cfg[f"{p}PTR"] = (u.cfg[f"{p}PTR"] + u.cfg[f"{p}INC"]) & (L1_WORDS - 1)
            s0, s1 = u.cfg[f"{p}STR"]
            n = _walk_words(f["mode"], f["n1"] + 1, name == "VPACK")
            words = [(base + (w % 4) * s0 + (w // 4) * s1) % L1_WORDS for w in range(n)]
            return words, name == "VPACK", what
        if name in ("VFILL", "VDRAIN"):
            n = u.walk_words(f["ad"])
            words = [(f["off"] + w) % L1_WORDS for w in range(n)]
            return words, name == "VFILL", what
        return None

    def _guard(self, name: str, f: dict):
        """A sync's or a wait's guard, its target fixed now (at dispatch)."""
        u = self.u
        if name == "VWAIT":
            c = f["counter"]
            u.target[c] += f["n"]
            want = u.target[c]
            ctr = u.rx if c == C.RX else u.cr
            return lambda: (ctr.reached(want), [ctr.clock_to(want)])
        if name == "VBAR":
            kind, upto = "F", self.count["F"]
        elif f.get("marked"):
            kind, upto = self.marks.get(f["mark"], ("F", 0))
        else:
            kind = KIND[f["on"]]
            upto = max(self.count[kind] - f["slack"], 0)
        return lambda: self.finished_upto(kind, upto)

    def _deps(self, e: _Entry) -> None:
        """Older register accesses `e` waits for (vector-core-v2.md s2)."""
        f = e.f
        if e.op in MATH:
            reads = self._reads(e.op, f)
            vd = f["vd"]
            for r in reads:
                e.deps += [x for x, a in self._live(r) if a in ("MW", "UW")]
            e.deps += [x for x, a in self._live(vd) if a in ("UW", "PR")]
            for r in reads:
                self.regs[r].append((e, "MR"))
            if not e.op.startswith("VCMP"):
                self.regs[vd].append((e, "MW"))
        elif e.op == "VUNPK":
            r = f["reg"]
            e.deps += [x for x, _ in self._live(r)]
            self.regs[r].append((e, "UW"))
        elif e.op == "VPACK":
            r = f["reg"]
            e.deps += [x for x, a in self._live(r) if a in ("MW", "UW", "PR")]
            self.regs[r].append((e, "PR"))

    def _live(self, r: int) -> list:
        live = [(x, a) for x, a in self.regs.get(r, ()) if x.done is None]
        self.regs[r] = live
        return live

    @staticmethod
    def _reads(op: str, f: dict) -> list:
        if op in UNARY:
            names = ["va"]
        elif op in READS_AC:
            names = ["va", "vc"]
        elif op in READS_AB:
            names = ["va", "vb"]
        else:
            names = ["va", "vb", "vc"]
        sel = {"va": "sa", "vb": "sb", "vc": "sc"}
        out = [f[n] for n in names if not f[sel[n]]]
        if f["om"]:
            out.append(f["vd"])  # a partial write keeps the rest: a read too
        return out

    def _check_block(self, pc: int, n: int) -> None:
        """Lemma 4: no VWAIT, VPOST or peer VDRAIN under a data-dependent skip."""
        for k in range(n):
            fmt, f = _decode(self.fetch(pc + k))
            if fmt is None or fmt.name not in ("VWAIT", "VPOST", "VDRAIN"):
                continue
            if fmt.name == "VDRAIN" and not (f["dst_x"] or f["dst_y"] or f["node"]):
                continue
            key = ("skip", pc + k)
            if key not in self.u.card.m.flagged:
                self.u.card.m.flagged.add(key)
                self.u.card.m.find("model", self.name, f"pc {pc + k}: {fmt.name} under a data-dependent skip")


class _Engine(Proc):
    def __init__(self, u: Vector, kind: str) -> None:
        super().__init__(u.card.m)
        self.u, self.kind = u, kind
        self.name = f"{u.name} {kind} engine"
        self.items: list = []
        self.kinds: list = []
        self.head = 0

    def done(self) -> bool:
        return self.head >= len(self.items)

    def blocked(self) -> str:
        e = self.items[self.head]
        return f"{e.op} waits"

    def step(self) -> bool:
        if self.head >= len(self.items):
            return False
        e = self.items[self.head]
        if any(x.done is None for x in e.deps):
            return False
        rel = [e.clock] + [x.done for x in e.deps]
        if e.guard is not None:
            ok, more = e.guard()
            if not ok:
                return False
            rel += more
        clock = self.tick(*rel)
        if e.l1 is not None:
            words, write, what = e.l1
            self.u.mem.access("L1", words, write, clock, self.ix, what)
        if e.op == "VPOST":
            self.u.card.post(self.u, (e.f["dst_x"], e.f["dst_y"]), e.f["n"], clock, self.ix)
        elif e.op == "VDRAIN" and (e.f["dst_x"] or e.f["dst_y"] or e.f["node"]):
            self._peer(e.f, e.roff, clock)
        e.done = clock
        kind = self.kinds[self.head]
        if kind is not None:
            self.u.fe.finished[kind].append(clock)
        self.head += 1
        return True

    def _peer(self, f: dict, roff: int, clock: Clock) -> None:
        """A peer drain: one burst of the walk's words; the descriptor's base
        (plus a `rel` drain's running offset) is {COUNT [34], fin [33:26],
        mesh [25:24], ack [23:16], offset [15:0]}."""
        u = self.u
        base = u.desc.get((f["ad"], 0), 0) + roff
        n = u.walk_words(f["ad"])
        if n > PEER_WORDS:
            u.card.m.find("bounds", self.name, f"a peer drain of {n} words faults (F_LEN)")
            return
        fin, ack = base >> 26 & 0xFF, base >> 16 & 0xFF
        sink = {
            "to": (f["dst_x"], f["dst_y"]),
            "far": (fin & 0xF, fin >> 4) if fin else None,
            "mesh": base >> 24 & 3,
            "buf": f["buf_id"],
            "count": bool(base >> 34 & 1),
            "signal": bool(f["signal"]),
            "ack": (ack & 0xF, ack >> 4),
        }
        u.card.send(u, sink, base & 0xFFFF, n, clock, self.ix)
