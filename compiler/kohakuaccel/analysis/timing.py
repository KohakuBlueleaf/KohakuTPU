"""How long a compiled kernel takes, in cycles, before anything runs.

ANALYTIC, not cycle-accurate. A stage costs its busiest unit and a kernel costs
the sum of its stages, because a stage IS a barrier.

The project supplies `cost(stmt, machine) -> cycles` and optionally
`hidden(stmt) -> bool` for work a unit overlaps with what precedes it; nothing
here knows a fill from a ray cast. The answer is a BRACKET: `cycles` charges
every statement, `overlapped` charges no hidden one, the machine is between.
"""

from dataclasses import dataclass, field


def deal(instances: int, units: int) -> list[int]:
    """Which unit each instance runs on, in contiguous blocks.

    A run of neighbouring instances per unit rather than every n-th one: a run
    is one descriptor and one hardware loop, a stride of `units` is neither.
    """
    live = max(1, min(instances, units))
    return [i * live // max(1, instances) for i in range(instances)]


@dataclass
class Stage:
    """What one stage cost, and where it went."""

    index: int
    unit: str
    cycles: int = 0
    hidden: int = 0
    per_unit: dict = field(default_factory=dict)
    by_kind: dict = field(default_factory=dict)

    @property
    def overlapped(self) -> int:
        return max(0, self.cycles - self.hidden)


@dataclass
class Timing:
    """Cycles for a whole kernel, per stage and per unit type."""

    stages: list = field(default_factory=list)
    mhz: float = 300.0

    @property
    def cycles(self) -> int:
        return sum(s.cycles for s in self.stages)

    @property
    def overlapped(self) -> int:
        return sum(s.overlapped for s in self.stages)

    @property
    def seconds(self) -> float:
        return self.cycles / (self.mhz * 1e6)

    @property
    def by_unit(self) -> dict:
        out: dict = {}
        for s in self.stages:
            out[s.unit] = out.get(s.unit, 0) + s.cycles
        return out

    @property
    def by_kind(self) -> dict:
        out: dict = {}
        for s in self.stages:
            for kind, n in s.by_kind.items():
                out[kind] = out.get(kind, 0) + n
        return out

    def share(self, unit: str) -> float:
        """Fraction of the critical path spent on `unit`.

        The denominator that keeps an optimisation honest: 20% off a part worth
        18% of the program is 3.6%.
        """
        return self.by_unit.get(unit, 0) / self.cycles if self.cycles else 0.0

    def occupancy(self, stage: Stage, units: int) -> float:
        """Fraction of a stage's span its units were busy, over `units` of them."""
        if not stage.cycles or not units:
            return 0.0
        return sum(stage.per_unit.values()) / (stage.cycles * units)

    def __str__(self) -> str:
        per = "  ".join(f"{k} {v:,}" for k, v in sorted(self.by_unit.items()))
        return (
            f"{self.overlapped:,}-{self.cycles:,} cycles "
            f"({self.overlapped / (self.mhz * 1e6) * 1e3:.3f}-"
            f"{self.seconds * 1e3:.3f} ms at {self.mhz:g} MHz)  {per}"
        )

    def report(self) -> str:
        lines = [str(self)]
        for s in self.stages:
            share = s.cycles / self.cycles if self.cycles else 0.0
            kinds = " ".join(f"{k} {v:,}" for k, v in sorted(s.by_kind.items()))
            lines.append(
                f"  stage {s.index:>2} {s.unit:<3} {s.cycles:>10,} cy "
                f"{share:>5.1%}  {kinds}"
            )
        return "\n".join(lines)


def time_of(
    compiled, cost, machine=None, hidden=None, mhz: float = 300.0, group=None
) -> Timing:
    """Cycles for `compiled`, charging each statement what `cost` says.

    `cost(stmt, machine)` returns cycles; `hidden(stmt)` marks work a unit
    overlaps with what precedes it. `machine` supplies `coords(unit)` so
    instances are dealt over the units that really exist; without it a stage is
    priced as one instance per unit.

    `group(compiled, stage, stmts)` partitions an instance's statements into the
    PROGRAMS they really run as, and `cost` is then handed each list. Without it
    every statement is priced alone, which cannot see a fusion: statements that
    become one program stop re-fetching each other's operands.
    """
    out = Timing(mhz=float(getattr(machine, "mhz", mhz)))
    for stage in compiled.stages:
        held = Stage(stage.index, stage.unit)
        try:
            units = len(machine.coords(stage.unit)) if machine else len(stage.instances)
        except (AttributeError, ValueError):
            units = len(stage.instances) or 1
        where = deal(len(stage.instances), max(1, units))
        for at, inst in enumerate(stage.instances):
            node = where[at] if at < len(where) else 0
            runs = (
                group(compiled, stage, inst.stmts)
                if group is not None
                else [[s] for s in inst.stmts]
            )
            for run in runs:
                n = int(cost(run, machine) or 0)
                held.per_unit[node] = held.per_unit.get(node, 0) + n
                held.by_kind[run[0].kind] = held.by_kind.get(run[0].kind, 0) + n
                if hidden is not None and all(hidden(s) for s in run):
                    held.hidden += n
        held.cycles = max(held.per_unit.values(), default=0)
        # The hidden share of the BUSIEST unit, not of every unit's total.
        held.hidden = held.hidden // max(1, len(held.per_unit))
        out.stages.append(held)
    return out


def compare(before: Timing, after: Timing) -> str:
    """`after` against `before`, per unit type and overall.

    The standing rule this exists to check: a new kernel is a defect unless it
    is faster than the one it replaces.
    """
    lines = []
    lo, hi = before.cycles or 1, after.cycles
    lines.append(f"  total   {lo:>12,} -> {hi:>12,}  {hi / lo:.2f}x")
    for unit in sorted(set(before.by_unit) | set(after.by_unit)):
        a = before.by_unit.get(unit, 0)
        b = after.by_unit.get(unit, 0)
        ratio = f"{b / a:.2f}x" if a else "new"
        lines.append(f"  {unit:<6}  {a:>12,} -> {b:>12,}  {ratio}")
    return "\n".join(lines)
