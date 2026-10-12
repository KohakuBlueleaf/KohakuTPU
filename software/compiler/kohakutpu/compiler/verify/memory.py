"""Races in a unit's local memory (verify.md s7).

Every access to a location is an event with a clock. Two accesses race when
one writes and neither happens before the other. Accesses arrive in the run's
order, which extends happens-before, so per location it suffices to keep the
last write and, per process, the last read since: a read that happens before
a later read of its own process is ordered against whatever that one is.
"""

from kohakutpu.compiler.verify.model import Clock


def before(a: tuple, b: tuple) -> bool:
    """Whether access `a` = (clock, proc, what) happens before access `b`."""
    return a[0].v.get(a[1], 0) <= b[0].v.get(a[1], 0)


class Memory:
    """One unit's local memory: per space (an L1 side), location -> state."""

    #: findings kept per unit
    LIMIT = 8

    def __init__(self, m, name: str) -> None:
        self.m, self.name = m, name
        self.writes: dict = {}  # (space, loc) -> access
        self.reads: dict = {}  # (space, loc) -> {proc: access}
        self.found = 0
        self.seen: set = set()

    def access(
        self, space, locs, write: bool, clock: Clock, proc: int, what: str
    ) -> None:
        e = (clock, proc, what)
        for loc in locs:
            key = (space, loc)
            w = self.writes.get(key)
            if w is not None and not before(w, e):
                self._race(key, w, e)
            if write:
                for r in self.reads.pop(key, {}).values():
                    if not before(r, e):
                        self._race(key, r, e)
                self.writes[key] = e
            else:
                self.reads.setdefault(key, {})[proc] = e

    def _race(self, key, a, b) -> None:
        pair = (a[2], b[2])
        if pair in self.seen or self.found >= self.LIMIT:
            return
        self.seen.add(pair)
        self.found += 1
        space, loc = key
        self.m.find(
            "race",
            self.name,
            f"{space} {loc}: {a[2]} and {b[2]} are not ordered",
        )
