"""Races in a unit's local memory (verify.md s7).

Every access to a location is an event with a clock. Two accesses race when
one writes and neither happens before the other. Accesses arrive in the run's
order, which extends happens-before, so per location it suffices to keep the
last write and, per process, the last read since: a read that happens before
a later read of its own process is ordered against whatever that one is.
"""

from kohakutpu.compiler.verify.model import Clock


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
        at = clock.at
        writes, reads = self.writes, self.reads
        # Access a = (clock, proc, what) happens before e iff e's clock has
        # seen a's event on a's process: a[0] at a[1] <= e's at a[1].
        for loc in locs:
            key = (space, loc)
            w = writes.get(key)
            if w is not None and w[0].at(w[1]) > at(w[1]):
                self._race(key, w, e)
            if write:
                rs = reads.pop(key, None)
                if rs:
                    for r in rs.values():
                        if r[0].at(r[1]) > at(r[1]):
                            self._race(key, r, e)
                writes[key] = e
            else:
                d = reads.get(key)
                if d is None:
                    reads[key] = {proc: e}
                else:
                    d[proc] = e

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
