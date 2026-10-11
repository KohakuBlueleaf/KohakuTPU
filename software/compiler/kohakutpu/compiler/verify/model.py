"""The abstract machine a package is checked on
(docs/projects/kohakutpu/verify.md).

Every process is sequential and every wait has one form: a counter that only
grows has reached a threshold fixed by the waiter's own history; what a
process spends it keeps as its own threshold. An enabled action therefore
stays enabled (Lemma 1) and every schedule ends in the same state
(Theorem 1): running one schedule to its end decides deadlock for all.
Each event carries a vector clock (§7.1).
"""

import bisect
from dataclasses import dataclass, field


class Clock:
    """A vector clock: process index -> events of that process seen."""

    __slots__ = ("v",)

    def __init__(self, v=None) -> None:
        self.v = dict(v or {})

    def join(self, other: "Clock") -> None:
        for k, n in other.v.items():
            if self.v.get(k, 0) < n:
                self.v[k] = n

    def copy(self) -> "Clock":
        return Clock(self.v)

    def before(self, other: "Clock", owner: int) -> bool:
        """Whether the event this clock stamps (on process `owner`) happens
        before the event `other` stamps."""
        return self.v.get(owner, 0) <= other.v.get(owner, 0)


class Counter:
    """A count that only grows, with the clock of every increment, kept per
    incrementing process (its owner).

    `clock_to(t)` is what reaching `t` must follow under EVERY schedule. With
    one owner that is its first increments up to `t`. With several, owner s
    contributes at least ``t - (total - total_s)`` of its own increments
    whatever the order (verify.md lemma 3), `total_s` its increments over the
    whole run; `totals` holds them from a first run (absent there, the
    first run joins its own order, which only that run's totals use)."""

    __slots__ = ("name", "value", "owners", "totals")

    def __init__(self, name: str, totals: dict | None = None) -> None:
        self.name = name
        self.value = 0
        #: owner -> [(owner's running total after the increment, its clock)]
        self.owners: dict = {}
        self.totals = totals

    def add(self, n: int, clock: Clock, owner: int) -> None:
        if n <= 0:
            return
        self.value += n
        marks = self.owners.setdefault(owner, [])
        marks.append(((marks[-1][0] if marks else 0) + n, clock.copy()))

    def reached(self, target: int) -> bool:
        return self.value >= target

    def total(self, owner: int) -> int:
        marks = self.owners.get(owner)
        return marks[-1][0] if marks else 0

    def clock_to(self, target: int, caps: dict | None = None) -> Clock:
        """The join of the increments reaching `target` must follow. `caps`
        bounds an owner's increments that can precede the wait (a unit's
        completions by the words already sent to it)."""
        out = Clock()
        if target <= 0:
            return out
        if self.totals is None:
            if len(self.owners) == 1:
                (marks,) = self.owners.values()
                _join_upto(out, marks, target)
            else:
                for marks in self.owners.values():
                    _join_upto(out, marks, target)
            return out
        avail = dict(self.totals)
        for owner, cap in (caps or {}).items():
            avail[owner] = min(avail.get(owner, 0), cap)
        whole = sum(avail.values())
        for owner, marks in self.owners.items():
            k = target - (whole - avail.get(owner, 0))
            if k > 0:
                _join_upto(out, marks, k)
        return out


def _join_upto(out: Clock, marks: list, k: int) -> None:
    """Join the clock of the first mark reaching `k`: one owner's clocks only
    grow, so it holds every earlier one."""
    i = bisect.bisect_left(marks, k, key=lambda m: m[0])
    out.join(marks[min(i, len(marks) - 1)][1])


@dataclass
class Finding:
    kind: str  # "deadlock" | "rest" | "source" | "port" | "race" | "model"
    where: str
    text: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.where}: {self.text}"


@dataclass
class Machine:
    """Counters by name, processes, and what the run found."""

    counters: dict = field(default_factory=dict)
    procs: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    events: int = 0
    #: counter name -> {owner: increments over the whole run}, from a first run
    totals: dict | None = None
    #: what a finding reported once is keyed by
    flagged: set = field(default_factory=set)

    def counter(self, name: str) -> Counter:
        c = self.counters.get(name)
        if c is None:
            t = None if self.totals is None else self.totals.get(name, {})
            c = self.counters[name] = Counter(name, t)
        return c

    def tally(self) -> dict:
        """Every counter's increments by owner: the next run's `totals`."""
        return {n: {o: c.total(o) for o in c.owners} for n, c in self.counters.items()}

    def add(self, proc) -> int:
        self.procs.append(proc)
        return len(self.procs) - 1

    def find(self, kind: str, where: str, text: str) -> None:
        self.findings.append(Finding(kind, where, text))

    def run(self, limit: int = 200_000_000) -> None:
        """Every process as far as it goes, round after round, until no
        process can take a step; then every process not at its end is in
        a deadlock (the same one under every schedule)."""
        while True:
            moved = False
            for p in self.procs:
                while p.step():
                    moved = True
                    self.events += 1
                    if self.events > limit:
                        self.find("model", "machine", f"over {limit} events")
                        return
            if not moved:
                # Stuck: a scheduling node may send past its blocked item.
                if not any(getattr(p, "relax", lambda: False)() for p in self.procs):
                    break
        stuck = [p for p in self.procs if not p.done()]
        for p in stuck:
            self.find("deadlock", p.name, p.blocked())


class Proc:
    """A sequential process: `step` takes its next action if its guard
    holds; `done` at its end; `blocked` says what its next action waits on."""

    name = "proc"

    def __init__(self, m: Machine) -> None:
        self.m = m
        self.ix = m.add(self)
        self.clock = Clock()
        self.n = 0

    def tick(self, *releases: Clock) -> Clock:
        """One event of this process, after everything `releases` saw."""
        for r in releases:
            self.clock.join(r)
        self.n += 1
        self.clock.v[self.ix] = self.n
        return self.clock.copy()

    def step(self) -> bool:
        raise NotImplementedError

    def done(self) -> bool:
        raise NotImplementedError

    def blocked(self) -> str:
        return "not at its end"


class Port(Proc):
    """A unit's receive port: no actions of its own; each burst landing is
    one of its events, so a landing has a clock apart from its sending."""

    def __init__(self, m: Machine, name: str) -> None:
        super().__init__(m)
        self.name = f"{name} port"

    def step(self) -> bool:
        return False

    def done(self) -> bool:
        return True
