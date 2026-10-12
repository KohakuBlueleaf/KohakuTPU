"""A matmul cluster on the abstract machine (`mx_cluster_cu.v`).

Two processes: the sequencer runs the unit's words in order; the drain queue
sends what sweeps emitted and drains sent. Counters: entries landed by COUNT
bursts per L1 side (taken by `fill recv`), credits received (spent by
bursts).

The write port closes a burst at 8 sub-tiles or whenever the queue runs dry,
so where bursts split depends on timing; a `credit` burst leaves only once
the credits hold its length. A burst holding sub-tile j of an item of g
starts at or after the item's start and ends before min(j + 8, g), so the
model lets sub-tile j leave once credits reach min(j + 8, g) past what the
item's predecessors spent: no split asks for more (verify.md lemma 5).
"""

import bisect
import functools

from kohakutpu.compiler.isa.cluster import DFLAG_COUNT, DFLAG_SIGNAL, ISA
from kohakutpu.compiler.verify.memory import Memory
from kohakutpu.compiler.verify.model import Clock, Port, Proc

#: Sub-tiles the drain queue holds before a sweep is held (DQ_DEPTH - 16;
#: DQ_DEPTH = max(TILES, 512), TILES 4096 on v9).
DQ_ROOM = 4096 - 16
BURST = 8
#: CU_DATA buffers and the granules each takes (GA = GB = 512 entries of 4
#: granules, TILES 4096 sub-tiles of 2 granules on v9).
BUF_L1A, BUF_L1B, BUF_PEER = 0, 1, 2
LIMIT = {BUF_L1A: 512 * 4, BUF_L1B: 512 * 4, BUF_PEER: 4096 * 2}


def _entries(bank: int, off: int, n: int) -> list:
    """L1 entries {bank, (off + i) mod 256}, i < n: an 8-bit offset and a
    ninth bank bit (`mx_cluster_cu.v` pl_ent)."""
    return [bank << 8 | ((off + i) & 0xFF) for i in range(n)]


@functools.lru_cache(maxsize=1 << 16)
def _decode(word: int) -> tuple:
    fmt = ISA.set.find(word)
    return (None, None) if fmt is None else (fmt, fmt.decode(word))


class Cluster:
    """The unit: its queue of words, its counters, its two processes."""

    kind = "MG"

    def __init__(self, card, key: tuple, name: str) -> None:
        self.card, self.key, self.name = card, key, name
        m = card.m
        self.words: list = []  # (word, clock) delivered by the node
        self.rx = {
            BUF_L1A: m.counter(f"rx:{name}:A"),
            BUF_L1B: m.counter(f"rx:{name}:B"),
        }
        self.taken = {BUF_L1A: 0, BUF_L1B: 0}
        self.cr = m.counter(f"cr:{name}")
        self.spent = 0
        self.done = m.counter(f"done:{name}")
        self.recv = m.counter(f"recv:{name}")
        self.acks = m.counter(f"acks:{name}")
        self.port = Port(m, name)
        #: the array: a sweep reads L1 from its issue until the array is
        #: idle, which the sequencer learns only at a POST or a DRAIN
        self.array = Port(m, f"{name} array")
        self.mem = Memory(m, name)
        self.seq = _Seq(self)
        self.dq = _Queue(self)
        #: the process whose events are this unit's completions
        self.completer = self.seq.ix
        #: bursts landed here: (sender proc, send clock, landing clock)
        self.landings: list = []
        #: L1 bursts: (landing clock, send clock, sender proc); memory FILLs'
        #: response windows: (start clock, end clock). FILL responses and
        #: CU_DATA words share one entry-assembly register (`mx_cluster_cu.v`
        #: L1 placement), so the two must never overlap.
        self.l1_bursts: list = []
        self.fill_windows: list = []

    def deliver(self, word: int, clock) -> None:
        self.words.append((word, clock))

    def land(
        self, buf: int, off: int, n: int, count: bool, clock, what: str, sent=None
    ) -> str:
        """`n` granules at granule `off` of buffer `buf`, sent at `sent`; a
        COUNT burst counts each entry whose last word lands. Returns why the
        cluster rejects it ("" when it takes it)."""
        if buf not in LIMIT:
            return f"buffer {buf} is none of the cluster's"
        if off + n - 1 >= LIMIT[buf] or (buf == BUF_PEER and off & 1):
            return f"granules {off}..{off + n - 1} past buffer {buf}"
        if buf in self.rx:
            entries = range(off // 4, (off + n - 1) // 4 + 1)
            self.mem.access("AB"[buf], entries, True, clock, self.port.ix, what)
            self.l1_bursts.append((clock, sent, what))
        else:
            # An add into the accumulator, sub-tile t granules 2t and 2t + 1.
            self.mem.access(
                "acc", range(off // 2, (off + n) // 2), True, clock, self.port.ix, what
            )
        if count and buf in self.rx:
            self.rx[buf].add((off + n) // 4 - off // 4, clock, self.port.ix)
        return ""

    def joins(self, buf: int) -> list:
        """What a burst into `buf` lands after: an add into the accumulator
        waits out a running sweep at its head (`peer_open`)."""
        return [self.array.clock] if buf == BUF_PEER else []

    def credit(self, n: int, clock, owner: int) -> None:
        self.cr.add(n, clock, owner)

    def rest(self) -> list:
        """What is left on this unit's counters once everything ran."""
        out = []
        for side, c in self.rx.items():
            if c.value != self.taken[side]:
                out.append(
                    f"{c.value - self.taken[side]} entries landed on side {'AB'[side]} and never taken"
                )
        if self.cr.value != self.spent:
            out.append(f"{self.cr.value - self.spent} credits received and never spent")
        return out


class _Seq(Proc):
    def __init__(self, u: Cluster) -> None:
        super().__init__(u.card.m)
        self.u = u
        self.name = f"{u.name} sequencer"
        self.pc = 0
        #: sub-tiles pushed into the drain queue so far, and through the last
        #: emitting sweep (what a fused drain waits for)
        self.pushed = 0
        self.emitted = 0
        #: a drain to a unit at the head: pushed, and where its last sub-tile is
        self._queued = False
        self._until = 0
        self.why = ""
        #: the array's clock at the last emitting sweep (what a fused drain,
        #: waiting for its sub-tiles, knows the array finished)
        self.swept = Clock()

    def done(self) -> bool:
        return self.pc >= len(self.u.words) and self.u.card.node_done(self.u)

    def blocked(self) -> str:
        if self.pc >= len(self.u.words):
            return "waiting for words from the node"
        return f"word {self.pc}: {self.why or 'blocked'}"

    def step(self) -> bool:
        u = self.u
        if self.pc >= len(u.words):
            return False
        word, given = u.words[self.pc]
        fmt, f = _decode(word)
        if fmt is None:
            u.card.m.find(
                "model", self.name, f"word {self.pc} decodes as no cluster instruction"
            )
            self.pc = len(u.words)
            return False
        rel = [given]
        # When a memory FILL's responses may start arriving.
        start = self.clock.copy()
        start.join(given)
        if fmt.name == "FILL":
            if f["recv"]:
                side = f["sel"]
                want = u.taken[side] + f["n"]
                if not u.rx[side].reached(want):
                    self.why = f"fill {'AB'[side]} recv n={f['n']}: {u.rx[side].value - u.taken[side]} entries landed"
                    return False
                rel.append(u.rx[side].clock_to(want))
                u.taken[side] = want
            elif f["npeer"]:
                ok, clock, lead = u.card.rendezvous(self, f)
                if not ok:
                    self.why = "shared fill: waiting for its sharers to reach theirs"
                    return False
                rel.append(clock)
                # The lead's read: its responses come from then on.
                start = lead
        elif fmt.name == "GEMM" and f["emit"]:
            # An emitting sweep starts once everything queued before has left
            # (S_GEMM waits !drain_busy), then retires on issue; its sub-tiles
            # follow as they complete.
            if u.dq.left < self.pushed:
                self.why = f"emitting sweep: {self.pushed - u.dq.left} earlier sub-tiles still to leave"
                return False
            rel.append(u.dq.clock_left(self.pushed))
            rel.append(self.swept)
        elif fmt.name == "DRAIN":
            if f["fuse"]:
                if u.dq.left < self.emitted:
                    self.why = f"fused drain: {self.emitted - u.dq.left} sub-tiles still to leave"
                    return False
                rel.append(u.dq.clock_left(self.emitted))
            else:
                # Retires once the write port has sent its last sub-tile.
                if not self._queued:
                    self._push(f, f["n"], given)
                if u.dq.left < self._until:
                    self.why = (
                        f"drain: {self._until - u.dq.left} sub-tiles still to leave"
                    )
                    return False
                rel.append(u.dq.clock_left(self._until))
                self._queued = False
        if fmt.name in ("POST", "GEMM") or (fmt.name == "DRAIN" and not f["fuse"]):
            # Each waits for the array to be idle, its sweeps' reads done; an
            # emitting sweep holding more than the drain queue's room stays
            # busy until enough of its sub-tiles left.
            need = self.pushed - DQ_ROOM
            if u.dq.left < need:
                self.why = (
                    f"the array waits for the drain queue: {need - u.dq.left} sub-tiles"
                )
                return False
            rel += [u.array.clock, u.dq.clock_left(need)]
        elif fmt.name == "DRAIN":
            rel.append(self.swept)
        clock = self.tick(*rel)
        if fmt.name == "FILL" and not f["recv"]:
            side = "AB"[f["sel"]]
            u.mem.access(
                side,
                _entries(f["fbank"], f["eoff"], f["n"]),
                True,
                clock,
                self.ix,
                f"FILL {side} word {self.pc}",
            )
            u.fill_windows.append((start, clock))
        elif fmt.name == "GEMM":
            at = u.array.tick(clock)
            what = f"GEMM word {self.pc}"
            u.mem.access(
                "A",
                _entries(f["abank"], f["aoff"], f["gm"] * f["nk"]),
                False,
                at,
                u.array.ix,
                what,
            )
            u.mem.access(
                "B",
                _entries(f["bbank"], f["boff"], f["gn"] * f["nk"]),
                False,
                at,
                u.array.ix,
                what,
            )
            u.mem.access("acc", range(f["gm"] * f["gn"]), True, at, u.array.ix, what)
            if f["emit"]:
                # Sub-tiles leave from the issue on; only the last implies
                # the sweep read everything.
                self.swept = at
                g = f["gm"] * f["gn"]
                u.dq.push(f, g, clock)
                self.pushed += g
                self.emitted = self.pushed
        if fmt.name == "POST":
            u.card.post(u, (f["dst_x"], f["dst_y"]), f["n"], clock, self.ix)
        self.pc += 1
        self.why = ""
        u.done.add(1, clock, self.ix)
        u.recv.add(1, clock, self.ix)
        return True

    def _push(self, f: dict, n: int, given) -> None:
        """A plain DRAIN of `n` sub-tiles, once the array is idle (S_DRAIN
        waits !gemm_busy); into another cluster's accumulator a sub-tile is
        two granules (`mx_cluster_node.v` OP_SEND)."""
        clock = self.tick(given, self.u.array.clock)
        self.u.mem.access(
            "acc", range(n), False, clock, self.ix, f"DRAIN word {self.pc}"
        )
        g = 2 * n if f["dnode"] and f["dbuf"] == BUF_PEER else n
        self.u.dq.push(f, g, clock)
        self.pushed += g
        self._queued = True
        self._until = self.pushed


class _Queue(Proc):
    """The drain queue: items in order, each to its sink."""

    def __init__(self, u: Cluster) -> None:
        super().__init__(u.card.m)
        self.u = u
        self.name = f"{u.name} drain queue"
        #: (sub-tiles, fields, clock pushed)
        self.items: list = []
        self.head = 0
        self.into = 0  # sub-tiles of the head item already sent
        self.left = 0
        self.marks: list = []  # (left after, clock)

    def push(self, f: dict, g: int, clock: Clock) -> None:
        self.items.append((g, f, clock))

    def clock_left(self, upto: int) -> Clock:
        if upto <= 0:
            return Clock()
        i = bisect.bisect_left(self.marks, upto, key=lambda m: m[0])
        return self.marks[min(i, len(self.marks) - 1)][1]

    def done(self) -> bool:
        return self.head >= len(self.items)

    def blocked(self) -> str:
        g, _, _ = self.items[self.head]
        need = self.u.spent + min(self.into + BURST, g)
        return f"sub-tile {self.into} of {g} waits for credits: {self.u.cr.value} of {need}"

    def step(self) -> bool:
        if self.head >= len(self.items):
            return False
        u = self.u
        g, f, pushed = self.items[self.head]
        rel = [pushed]
        upto = g
        if f["dnode"] and f["credit"]:
            have = u.cr.value - u.spent
            upto = g if have >= g else have - (BURST - 1)
            if upto <= self.into:
                return False
            rel.append(u.cr.clock_to(u.spent + min(upto + BURST - 1, g)))
        clock = self.tick(*rel)
        n = upto - self.into
        if f["dnode"]:
            u.card.burst(
                u, f, ((f["addr"] >> 5) + self.into) & 0xFFFF, n, clock, self.ix
            )
        self.into = upto
        self.left += n
        self.marks.append((self.left, clock))
        if self.into >= g:
            if f["dnode"] and f["credit"]:
                u.spent += g
            self.head += 1
            self.into = 0
        return True


def burst_fields(f: dict) -> dict:
    """A drain's sink, as the card routes it."""
    return {
        "to": (f["dst_x"], f["dst_y"]),
        "far": (f["dfin"] & 0xF, f["dfin"] >> 4) if f["dfin"] else None,
        "mesh": f["dmesh"],
        "buf": f["dbuf"],
        "count": bool(f["dflags"] & DFLAG_COUNT),
        "signal": bool(f["dflags"] & DFLAG_SIGNAL),
        "ack": (f["dack_x"], f["dack_y"]),
    }
