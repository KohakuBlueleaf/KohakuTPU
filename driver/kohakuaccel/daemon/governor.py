"""The clock policy: low is the resting state, boosts are scoped leases.

The card has no proper cooling, so a mesh's clocks sit at the board's
idle profile whenever nothing holds them up. A run declares the level it
wants and the meshes it will touch; ONLY those meshes are raised, held
by a token, and dropped back after release once the idle window passes.
Reprogramming resets every MMCM to its BUILT full-speed configuration,
so the post-program hook re-applies the idle profile before anything
else is allowed to touch the card.

The governor is policy only. It never talks to hardware directly: every
retune goes through the daemon's serialized op queue via the injected
`clock_ctl`, an adapter with `nmesh`, `levels`, `apply(mesh, level)` and
`read(mesh)` -- which is also what keeps this module framework-clean
(the concrete wizard driver is project code).
"""

import time


class ClockGovernor:
    def __init__(self, clock_ctl, idle_seconds: float = 10.0) -> None:
        self.ctl = clock_ctl
        self.idle_seconds = idle_seconds
        self.idle_level = clock_ctl.idle_level
        #: mesh -> level currently applied, as far as policy knows.
        self.current = {m: None for m in range(clock_ctl.nmesh)}
        #: token -> (set of meshes, level)
        self.runs: dict[int, tuple[set, str]] = {}
        #: mesh -> monotonic time of last release/op touching it.
        self.last_active = {m: time.monotonic() for m in range(clock_ctl.nmesh)}
        self._next_token = 1

    def to_idle(self, force: bool = False) -> dict:
        """Every unheld mesh to the idle profile. `force` retunes even
        meshes already believed idle -- the post-program state is BUILT
        full speed whatever this policy last applied, so belief is void."""
        held = set()
        for meshes, _ in self.runs.values():
            held |= meshes
        done = {}
        for m in range(self.ctl.nmesh):
            if m in held:
                continue
            if force or self.current[m] != self.idle_level:
                done[m] = self.ctl.apply(m, self.idle_level)
                self.current[m] = self.idle_level
        return done

    def run_begin(self, meshes, level: str) -> int:
        if level not in self.ctl.levels:
            raise ValueError(f"no profile {level!r}; board has {self.ctl.levels}")
        bad = [m for m in meshes if not 0 <= m < self.ctl.nmesh]
        if bad:
            raise ValueError(f"no mesh {bad}; board has {self.ctl.nmesh}")
        token = self._next_token
        self._next_token += 1
        self.runs[token] = (set(meshes), level)
        for m in meshes:
            if self.current[m] != level:
                self.ctl.apply(m, level)
                self.current[m] = level
            self.last_active[m] = time.monotonic()
        return token

    def run_end(self, token: int) -> None:
        meshes, _ = self.runs.pop(token)
        now = time.monotonic()
        for m in meshes:
            self.last_active[m] = now
        # NOT dropped here: back-to-back runs would thrash the wizards.
        # The idle tick drops them once the window passes.

    def tick(self) -> dict:
        """The periodic idle check; returns whatever it retuned."""
        held = set()
        for meshes, _ in self.runs.values():
            held |= meshes
        now = time.monotonic()
        done = {}
        for m in range(self.ctl.nmesh):
            if m in held or self.current[m] == self.idle_level:
                continue
            if now - self.last_active[m] >= self.idle_seconds:
                done[m] = self.ctl.apply(m, self.idle_level)
                self.current[m] = self.idle_level
        return done

    def drop_runs_of(self, tokens) -> None:
        """Client death: its runs end; the idle tick handles the clocks."""
        for t in list(tokens):
            if t in self.runs:
                self.run_end(t)

    def status(self) -> dict:
        return {
            "current": dict(self.current),
            "runs": {t: (sorted(m), lv) for t, (m, lv) in self.runs.items()},
            "idle_seconds": self.idle_seconds,
            "idle_level": self.idle_level,
        }
