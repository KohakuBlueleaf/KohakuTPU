"""The simulator, at three levels, behind one door.

All three read the SAME compiled artifact -- the bytes the driver would dispatch
-- so they cannot disagree about what ran, only how much detail they keep.

    NUMBERS  MXFP7, ACC24, E8M15 and every saturation. `kohakutpu.model`.
    CYCLES   analytic, and a BRACKET: a stage costs its busiest unit, and a
             fill hides behind the GEMM ahead of it. `analysis.timing`.
    MESH     dispatches, rounds, flits, host traffic, relayouts and each unit's
             instruction mix. The level that sees a MAG round trip.

:func:`observe` runs once and returns all three.
"""

from dataclasses import dataclass, field

import numpy as np
from kohakutpu.cost import time as _time
from kohakutpu.model import SimDevice

#: Counters worth showing: what left the host, and what the mesh did with it.
COUNTERS = ("dispatches", "rounds", "flits", "sent", "fetched", "relayouts")


@dataclass
class Trace:
    """One run of one kernel, seen at all three levels."""

    result: np.ndarray
    compiled: object
    timing: object
    counters: dict = field(default_factory=dict)
    clusters: dict = field(default_factory=dict)
    cores: dict = field(default_factory=dict)
    saturated: int = 0

    @property
    def stages(self) -> int:
        """Top-level stages -- how many times the result goes back through MAG."""
        return len(self.compiled.stages)

    @property
    def temps(self) -> list:
        """Every buffer the kernel had to allocate that is not an operand."""
        return sorted(self.compiled.temps)

    def report(self) -> str:
        """All three levels, as lines."""
        out = [
            (
                f"{self.compiled.name}  {self.stages} stages, "
                f"{len(self.temps)} temp(s) {self.temps}"
            ),
            f"  CYCLES  {self.timing}",
        ]
        out += [f"    {line}" for line in self.timing.report().splitlines()[1:]]
        mesh = "  ".join(f"{k} {self.counters.get(k, 0):,}" for k in COUNTERS)
        out.append(f"  MESH    {mesh}")
        out.append(f"    clusters {_mix(self.clusters)}")
        out.append(f"    cores    {_mix(self.cores)}")
        out.append(f"  NUMBERS saturated to the fp16 ceiling: {self.saturated}")
        return "\n".join(out)


def _mix(counts: dict) -> str:
    """An instruction mix as `KIND n` pairs, or a note that nothing ran."""
    live = {k: v for k, v in sorted(counts.items()) if v}
    return "  ".join(f"{k} {v:,}" for k, v in live.items()) or "(idle)"


def device(**kw) -> SimDevice:
    """A simulated machine. `size=` overrides the arena `model.ARENA_SIZE` sets.

    Deliberately does NOT restate the default: a second one here made the same
    simulated machine 64 MiB through `sim` and 16 MiB through `api.device()`.
    """
    return SimDevice(**kw)


def observe(kern, *arrays, dev=None, **knobs) -> Trace:
    """Run `kern` on host arrays and return all three levels as a :class:`Trace`.

    `arrays` are plain numpy; they are uploaded here, so a caller needs no
    device of its own unless it wants to reuse one.
    """
    dev = device() if dev is None else dev
    held = [dev.tensor(a) for a in arrays]
    before = {k: dev.counters.get(k, 0) for k in COUNTERS}
    compiled = kern.plan(*held, **knobs)
    got = kern(*held, **knobs)
    return Trace(
        result=np.asarray(got.numpy()),
        compiled=compiled,
        timing=_time(compiled, dev.machine),
        counters={k: dev.counters.get(k, 0) - before[k] for k in COUNTERS},
        clusters=_total(dev.clusters),
        cores=_total(dev.cores),
        saturated=dev.saturated,
    )


def cycles(kern, *arrays, dev=None, **knobs):
    """CYCLES only: the analytic timing of `kern` at this shape.

    Compiles but does not run, so it costs no arithmetic -- which is what makes
    it the level to sweep a knob with.
    """
    dev = device() if dev is None else dev
    compiled = kern.plan(*[dev.tensor(a) for a in arrays], **knobs)
    return _time(compiled, dev.machine)


def _total(units: list) -> dict:
    """One instruction mix summed over a list of unit models."""
    out: dict = {}
    for unit in units:
        for kind, n in getattr(unit, "counts", {}).items():
            out[kind] = out.get(kind, 0) + n
    return out
