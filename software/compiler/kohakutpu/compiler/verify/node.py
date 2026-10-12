"""The node on the abstract machine: a package's steps under the firmware's
dispatch rules (`firmware/kohakuaccel/dispatch/engine.c`, mirrored by
`kohakuaccel.compiler.package.engine`).

* A word goes to a unit only while the unit holds fewer than its credit in
  flight (sent less completed); a unit with a fetch port takes its words in
  requests of at most min(room, 255), room = min(credit, 512), each only
  once all of it fits.
* `AWAIT u n` raises u's expected count by n and waits until u's
  completions (acknowledgements included) reach it; `BARRIER` and a
  package's end wait until every word sent has completed.

Consecutive DISPATCH / REPEAT steps form a segment: the words of different
units in it are unordered (they run on different units), each unit's in
order. A node made `free` is the dispatch scheduler (verify.md s6): it runs
a segment in package order, and only when the whole machine is stuck sends
the earliest item heading its unit's queue that can move; `packages()`
returns the packages in the order it sent.
"""

from dataclasses import replace

from kohakuaccel.compiler.package.format import (
    F_ACK,
    F_CONSUME,
    F_IMM,
    F_MEM,
    Alu,
    Cond,
    Op,
    Step,
)
from kohakutpu.compiler.verify.model import Proc

FETCH_DEPTH = 512
#: The node-wide cap (boot `cq_depth`, 0: none) the card boots with.
CAP = 1 << 32
MASK = (1 << 64) - 1
PATCH_STEPS = 1 << 24


def _signed(v: int) -> int:
    return v - (1 << 64) if v >> 63 else v


def _alu(op: int, a: int, b: int) -> int:
    f = {
        Alu.MOV: lambda: b,
        Alu.ADD: lambda: a + b,
        Alu.SUB: lambda: a - b,
        Alu.MUL: lambda: a * b,
        Alu.AND: lambda: a & b,
        Alu.OR: lambda: a | b,
        Alu.XOR: lambda: a ^ b,
        Alu.SHL: lambda: a << (b & 63),
        Alu.SHR: lambda: a >> (b & 63),
        Alu.MIN: lambda: min(a, b),
        Alu.MAX: lambda: max(a, b),
        Alu.DIVU: lambda: a // b if b else MASK,
        Alu.REMU: lambda: a % b if b else a,
    }[Alu(op)]
    return f() & MASK


def _cond(c: int, a: int, b: int) -> bool:
    return {
        Cond.EQ: a == b,
        Cond.NE: a != b,
        Cond.LTU: a < b,
        Cond.GEU: a >= b,
        Cond.LT: _signed(a) < _signed(b),
        Cond.GE: _signed(a) >= _signed(b),
    }[Cond(c)]


class _Item:
    """A DISPATCH (or a REPEAT, written out, sent whole) of one segment."""

    __slots__ = ("arg", "atomic", "count", "sent", "step", "unit")

    def __init__(
        self, unit: int, arg: int, count: int, atomic: bool, step: Step
    ) -> None:
        self.unit, self.arg, self.count = unit, arg, count
        self.atomic, self.step = atomic, step
        self.sent = 0


class _Segment:
    op = "SEGMENT"

    def __init__(self, pkg: int, start: int, base: int) -> None:
        self.pkg, self.start, self.end = pkg, start, start
        self.base = base  # the package's first payload in the node's list
        self.items: list = []
        #: one per run of the segment: [[item index, first word, words]]
        self.logs: list = []


class Node(Proc):
    def __init__(self, card, mesh: int, pkgs: list, free: bool = False) -> None:
        """`pkgs` run one after another (a package, then its rerun)."""
        super().__init__(card.m)
        self.card, self.mesh, self.pkgs, self.free = card, mesh, pkgs, free
        self.name = f"node {mesh}"
        card.nodes[mesh] = self
        pkg = pkgs[0]
        if any(p.units != pkg.units for p in pkgs):
            card.m.find("model", self.name, "the packages name different units")
        self.units = [card.unit((mesh, u.x, u.y)) for u in pkg.units]
        for u, spec in zip(self.units, pkg.units):
            if u is None:
                card.m.find(
                    "route",
                    self.name,
                    f"the package names ({spec.x},{spec.y}), where no unit is",
                )
                continue
            card.feed(self, u)
        self.credit = [u.credit or CAP for u in pkg.units]
        self.fetch = [bool(u.fetch >> 16) for u in pkg.units]
        self.sent = [0] * len(self.units)
        self.expected = [0] * len(self.units)
        self.payloads: list = []
        self.steps: list = []
        self.segments: list = []
        for i, p in enumerate(pkgs):
            self._load(p, i)
        self.at = 0
        self.armed = False  # the current AWAIT / SEND raised its count
        self.why = ""
        #: package registers: an int, or None once it holds data from memory
        self.regs: dict = {}
        #: JUMP step -> times taken on data
        self.taken_on_data: dict = {}
        self.rx = card.m.counter(f"rx:{self.name}")
        self.rx_taken = 0
        #: where bursts the node sends come from, as a unit's key
        self.key = (mesh, *card.ports(mesh)[0])
        self.acks_taken = [0] * len(self.units)
        #: (this node's event count, clock) at each package's end
        self.ends: list = []
        self._log = None
        #: the run whose end every node must reach before this one goes on
        self.host = 0

    # ------------------------------------------------------------- loading
    def _load(self, pkg, index: int) -> None:
        """The package's steps with each segment one step, and every payload
        and step index moved past the packages before."""
        base = len(self.payloads)
        self.payloads += pkg.payloads
        first = len(self.steps)
        where = {}  # package step -> its step here
        seg = None
        jumps = []
        for j, s in enumerate(pkg.steps):
            if s.op in (Op.DISPATCH, Op.REPEAT):
                if seg is None:
                    seg = _Segment(index, j, base)
                    self.segments.append(seg)
                    self.steps.append(seg)
                where[j] = len(self.steps) - 1
                seg.end = j + 1
                if s.op == Op.DISPATCH:
                    seg.items.append(_Item(s.unit, base + s.arg, s.count, False, s))
                else:
                    at, n = self._repeat(s, base)
                    seg.items.append(_Item(s.unit, at, n, True, s))
                continue
            seg = None
            where[j] = len(self.steps)
            if s.op == Op.JUMP:
                jumps.append(len(self.steps))
            if s.op in (Op.SEND, Op.RECV, Op.LOAD):
                s = Step(s.op, s.unit, s.count, s.arg + base, s.flags)
            self.steps.append(s)
        if not self.steps[first:] or getattr(self.steps[-1], "op", None) != Op.END:
            where[len(pkg.steps)] = len(self.steps)
            self.steps.append(Step(Op.END))
        for k in jumps:
            s = self.steps[k]
            self.steps[k] = Step(Op.JUMP, where[s.unit], s.count, s.arg, s.flags)

    def _repeat(self, s, base: int) -> tuple:
        """A REPEAT's words written out: (first payload, words)."""
        payloads = self.payloads
        first, repeats, nincs = (
            base + (s.arg & 0xFFFF_FFFF),
            (s.arg >> 32) & 0xFFFF,
            s.arg >> 48,
        )
        incs = []
        for k in range(nincs):
            w = payloads[first + s.count + k // 2] >> (128 * (k % 2))
            head, delta = w & MASK, (w >> 64) & MASK
            incs.append(
                (head & 0xFFFF_FFFF, (head >> 32) & 0xFF, (head >> 40) & 0xFF, delta)
            )
        at = len(payloads)
        for r in range(repeats):
            for j in range(s.count):
                w = payloads[first + j]
                for index, bit, width, delta in incs:
                    if index == j:
                        mask = (1 << width) - 1
                        w = (w & ~(mask << bit)) | (
                            (((w >> bit) & mask) + r * delta) & mask
                        ) << bit
                payloads.append(w)
        return at, repeats * s.count

    # ------------------------------------------------------------- the run
    def done(self) -> bool:
        return self.at >= len(self.steps)

    def blocked(self) -> str:
        s = self.steps[self.at]
        if isinstance(s, _Segment):
            return f"segment at package {s.pkg} step {s.start}: {self.why}"
        return f"step {self.at} {s.op.name} u{s.unit}: {self.why}"

    def retired(self, u: int) -> int:
        return self.units[u].done.value

    def step(self) -> bool:
        if self.at >= len(self.steps):
            return False
        if self.host:
            # The host submits the next run once every node ended this one
            # (`VerilatorCard.run` waits for each node's package).
            c = self.card.m.counter(f"host:{self.host}")
            if not c.reached(len(self.card.nodes)):
                self.why = "the host waits for every node's package"
                return False
            self.tick(c.clock_to(len(self.card.nodes)))
            self.host = 0
        s = self.steps[self.at]
        if isinstance(s, _Segment):
            return self._segment(s)
        op = s.op
        if op in (Op.END, Op.BARRIER):
            # BARRIER, and the package's end (interp.c: ka_engine_barrier):
            # every word sent has completed.
            rel = []
            for u, unit in enumerate(self.units):
                if unit is None:
                    continue
                if not unit.done.reached(self.sent[u]):
                    self.why = f"barrier: {unit.name} {unit.done.value} of {self.sent[u]} completed"
                    return False
                rel.append(unit.done.clock_to(self.sent[u]))
            clock = self.tick(*rel)
            if op == Op.END:
                # The next package counts its completions from here.
                self.expected = [max(e, n) for e, n in zip(self.expected, self.sent)]
                self.ends.append((self.n, clock))
                self.regs = {}
                self.card.m.counter(f"host:{len(self.ends)}").add(1, clock, self.ix)
                self.at += 1
                self.host = len(self.ends)
                return True
            self.at += 1
            return True
        if op == Op.AWAIT:
            u = s.unit
            unit = self.units[u]
            if unit is None:
                self.at += 1
                return True
            if not self.armed:
                self.expected[u] += s.count
                self.armed = True
            if not unit.recv.reached(self.expected[u]):
                self.why = (
                    f"{unit.name}: {unit.recv.value} of {self.expected[u]} completions"
                )
                return False
            # The unit completes at most the words this node sent it so far.
            self.tick(
                unit.recv.clock_to(self.expected[u], {unit.completer: self.sent[u]})
            )
            self.armed = False
            self.at += 1
            return True
        if op == Op.RECV:
            want = self.rx_taken + s.count
            if not self.rx.reached(want):
                self.why = f"recv: {self.rx.value - self.rx_taken} of {s.count} flits"
                return False
            self.tick(self.rx.clock_to(want))
            self.rx_taken = want
            self.at += 1
            return True
        if op in (Op.RING, Op.WAIT_BELL, Op.WAIT_SLOT, Op.FETCH, Op.SEND, Op.ENGINE):
            return self._other(s)
        if op in (Op.LOAD, Op.ALU, Op.JUMP):
            return self._register(s)
        # MOVER, MWAIT, SETTLE, SIGNAL, CTRL, STORE, PATCH: nothing a unit
        # waits on.
        self.tick()
        self.at += 1
        return True

    # ------------------------------------------------------------- segments
    def _segment(self, seg: _Segment) -> bool:
        """Package order: the first item not fully sent, as far as credit lets."""
        if self._log is None:
            for it in seg.items:
                it.sent = 0
            self._log = []
        moved = False
        for i, it in enumerate(seg.items):
            if it.sent < it.count:
                n = self._push(i, it)
                moved = moved or n > 0
                if it.sent < it.count:
                    return moved
        seg.logs.append(self._log)
        self._log = None
        self.at += 1
        return True

    def relax(self) -> bool:
        """The machine is stuck: send what the earliest item heading its
        unit's queue can, past the blocked one. False if none can move."""
        if not self.free or self.at >= len(self.steps):
            return False
        seg = self.steps[self.at]
        if not isinstance(seg, _Segment) or self._log is None:
            return False
        if any(it.atomic and 0 < it.sent < it.count for it in seg.items):
            return False
        heads, first = set(), True
        for i, it in enumerate(seg.items):
            if it.sent >= it.count or it.unit in heads:
                continue
            heads.add(it.unit)
            if first:
                first = False
                continue
            if self._push(i, it):
                return True
        return False

    def _push(self, i: int, it: _Item) -> int:
        """Words of item `i` sent now under the dispatch rules."""
        u = it.unit
        unit = self.units[u]
        if unit is None:
            it.sent = it.count
            return 0
        total = 0
        while it.sent < it.count:
            if self.fetch[u]:
                limit = min(self.credit[u], FETCH_DEPTH)
                n = min(it.count - it.sent, max(1, min(limit, 255)))
            else:
                limit, n = self.credit[u], 1
            need = self.sent[u] + n - limit
            if need > self.retired(u):
                self.why = f"{n} words to {unit.name}: {self.sent[u] - self.retired(u)} in flight"
                break
            clock = self.tick(unit.done.clock_to(need)) if need > 0 else self.tick()
            for j in range(n):
                unit.deliver(self.payloads[it.arg + it.sent + j], clock)
            log = self._log
            if log and log[-1][0] == i:
                log[-1][2] += n
            else:
                log.append([i, it.sent, n])
            self.sent[u] += n
            it.sent += n
            total += n
        return total

    def packages(self) -> list:
        """The packages with each segment's items in the order sent. Raises
        :class:`ValueError` when a segment run twice (a loop) was sent in two
        orders."""
        out = []
        for index, pkg in enumerate(self.pkgs):
            segs = {s.start: s for s in self.segments if s.pkg == index}
            steps, where, j = [], {}, 0
            while j < len(pkg.steps):
                seg = segs.get(j)
                if seg is None:
                    where[j] = len(steps)
                    steps.append(pkg.steps[j])
                    j += 1
                    continue
                for k in range(seg.start, seg.end):
                    where[k] = len(steps)
                steps += self._order(seg)
                j = seg.end
            where[len(pkg.steps)] = len(steps)
            for k, s in enumerate(steps):
                if s.op == Op.JUMP:
                    steps[k] = Step(Op.JUMP, where[s.unit], s.count, s.arg, s.flags)
                elif s.op == Op.PATCH and s.arg & PATCH_STEPS:
                    steps[k] = Step(Op.PATCH, s.unit, where[s.count], s.arg, s.flags)
            out.append(replace(pkg, steps=steps))
        return out

    def _order(self, seg: _Segment) -> list:
        if not seg.logs:
            return [it.step for it in seg.items]
        if any(log != seg.logs[0] for log in seg.logs):
            raise ValueError(
                f"node {self.mesh}: a segment in a loop was sent in two orders"
            )
        out = []
        for i, first, n in seg.logs[0]:
            it = seg.items[i]
            if it.atomic:
                out.append(it.step)
            else:
                out.append(Step(Op.DISPATCH, it.unit, n, it.step.arg + first))
        return out

    # ------------------------------------------------------------- the rest
    def _other(self, s) -> bool:
        m = self.card.m
        if s.op == Op.RING:
            clock = self.tick()
            m.counter(f"slot:{s.unit}:{s.count}").add(s.arg & 0xFFFF, clock, self.ix)
            m.counter(f"bell:{s.unit}:{self.mesh}").add(1, clock, self.ix)
            self.at += 1
            return True
        if s.op in (Op.WAIT_BELL, Op.WAIT_SLOT):
            if s.op == Op.WAIT_BELL:
                key, c = ("bell", s.unit), m.counter(f"bell:{self.mesh}:{s.unit}")
            else:
                key, c = ("slot", s.unit), m.counter(f"slot:{self.mesh}:{s.unit}")
            self.card.waited.add((self.mesh, key))
            taken = self.card.taken.get((self.mesh, key), 0)
            want = taken + s.count
            if not c.reached(want):
                self.why = f"{key[0]} {key[1]}: {c.value - taken} of {s.count}"
                return False
            self.tick(c.clock_to(want))
            if s.op == Op.WAIT_BELL or s.flags & F_CONSUME:
                self.card.taken[(self.mesh, key)] = want
            self.at += 1
            return True
        if s.op == Op.SEND:
            return self._send(s)
        self.card.m.find(
            "model", self.name, f"step {self.at}: {s.op.name} is not modelled"
        )
        self.tick()
        self.at += 1
        return True

    def _register(self, s) -> bool:
        """LOAD, ALU, JUMP (interp.c load/alu/jump). A loaded value is data;
        a JUMP on data is taken `card.trips` times, then not."""
        self.tick()
        r = self.regs
        if s.op == Op.LOAD:
            r[s.unit] = None
            self.at += 1
            return True
        a = r.get((s.count >> 8) & 0xF, 0)
        b = s.arg if s.flags & F_IMM else r.get((s.count >> 12) & 0xF, 0)
        if s.op == Op.ALU:
            r[s.unit] = None if a is None or b is None else _alu(s.count & 0xFF, a, b)
            self.at += 1
            return True
        cond = s.count & 0xFF
        if cond == Cond.ALWAYS:
            go = True
        elif a is None or b is None:
            n = self.taken_on_data.get(self.at, 0)
            go = n < self.card.trips
            self.taken_on_data[self.at] = n + (1 if go else 0)
            if not go:
                self.card.m.find(
                    "loop",
                    self.name,
                    f"step {self.at}: a loop on data, checked for 0..{self.card.trips} trips",
                )
        else:
            go = _cond(cond, a, b)
        self.at = s.unit if go else self.at + 1
        return True

    def _send(self, s) -> bool:
        """Raw flits to a unit (interp.c send): a CU_DATA descriptor and its
        words, the words read from memory under F_MEM; with F_ACK the step
        waits for one more DATA_RECEIVED from the unit."""
        unit = self.units[s.unit]
        if not self.armed:
            kind = (s.arg >> 32) & 0xF
            if kind == 8 and unit is not None:
                d = self.payloads[s.arg & 0xFFFF_FFFF]
                n = s.count if s.flags & F_MEM else s.count - 1
                ack = (d >> 208) & 0xFF
                sink = {
                    "to": unit.key[1:],
                    "far": None,
                    "mesh": self.mesh,
                    "buf": (d >> 248) & 0xFF,
                    "count": bool((d >> 216) & 2),
                    "signal": bool((d >> 216) & 1),
                    "ack": (ack & 0xF, ack >> 4),
                }
                if n != ((d >> 224) & 0xFF) + 1:
                    self.card.m.find(
                        "bounds",
                        self.name,
                        f"step {self.at}: {n} words under a descriptor of {((d >> 224) & 0xFF) + 1}",
                    )
                self.card.send(self, sink, (d >> 232) & 0xFFFF, n, self.tick(), self.ix)
            elif kind == 7 and unit is not None:
                # CU_CTRL: the unit's control port answers each flit with one.
                self.rx.add(s.count, self.tick(), unit.port.ix)
            else:
                self.tick()
            self.armed = True
            if s.flags & F_ACK and unit is not None:
                self.acks_taken[s.unit] += 1
        if s.flags & F_ACK and unit is not None:
            want = self.acks_taken[s.unit]
            if not unit.acks.reached(want):
                self.why = f"send: DATA_RECEIVED from {unit.name}"
                return False
            self.tick(unit.acks.clock_to(want))
        self.armed = False
        self.at += 1
        return True

    def rest(self) -> list:
        """What the node leaves behind: flits no RECV took."""
        out = []
        if self.rx.value != self.rx_taken:
            out.append(
                f"{self.rx.value - self.rx_taken} flits landed here and never received"
            )
        return out
