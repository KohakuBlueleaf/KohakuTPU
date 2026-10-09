"""The L2 schedule: buffers, views, work items in sequence order, placement and
packages (docs/spec/l2-schedule.md). Dependences are derived from the views,
never declared; the checker refuses what would lower to a wrong program."""

from dataclasses import dataclass, field


class ScheduleError(ValueError):
    """A schedule the checker refuses."""


def _frozen(v):
    """A parameter value with every list a tuple: one value, one form."""
    return tuple(map(_frozen, v)) if isinstance(v, (list, tuple)) else v


@dataclass(eq=False)
class Buffer:
    """Bytes with a project layout tag. `space` is ``"mem"`` or a unit's local
    storage ``("local", coord)``; a memory buffer's `base` is set by its maker
    or by allocation."""

    name: str
    nbytes: int
    layout: object = None
    space: object = "mem"
    base: int | None = None

    def view(self, offset: int = 0, nbytes: int | None = None) -> "View":
        return View(self, offset, self.nbytes - offset if nbytes is None else nbytes)

    @property
    def local(self):
        """The coordinate whose storage this is, or None for memory."""
        return self.space[1] if isinstance(self.space, tuple) else None


@dataclass(frozen=True)
class View:
    buffer: Buffer
    offset: int
    nbytes: int

    @property
    def end(self) -> int:
        return self.offset + self.nbytes

    @property
    def address(self) -> int:
        if self.buffer.base is None:
            raise ScheduleError(f"{self.buffer.name} has no address yet")
        return self.buffer.base + self.offset

    def overlaps(self, other: "View") -> bool:
        return (
            self.buffer is other.buffer
            and self.offset < other.end
            and other.offset < self.end
        )


@dataclass
class Item:
    """One unit of work of a project `kind` on one unit TYPE."""

    kind: str
    unit: str
    params: dict = field(default_factory=dict)
    reads: tuple = ()
    writes: tuple = ()
    at: tuple | None = None
    package: int = 0


@dataclass
class Schedule:
    """Buffers and the items in sequence order; `machine`, when known, the one
    its placement names."""

    buffers: list = field(default_factory=list)
    items: list = field(default_factory=list)
    machine: object = None

    def buffer(self, name, nbytes, layout=None, space="mem", base=None) -> Buffer:
        b = Buffer(name, nbytes, layout, space, base)
        self.buffers.append(b)
        return b

    def add(
        self, kind, unit, params=None, reads=(), writes=(), at=None, package=0
    ) -> int:
        self.items.append(
            Item(
                kind,
                unit,
                {k: _frozen(v) for k, v in (params or {}).items()},
                tuple(reads),
                tuple(writes),
                None if at is None else tuple(at),
                package,
            )
        )
        return len(self.items) - 1

    def deps(self) -> list[set]:
        """``deps[b]``: the earlier items `b` must follow (RAW, WAW, WAR)."""
        out = [set() for _ in self.items]
        for b, ib in enumerate(self.items):
            for a in range(b):
                ia = self.items[a]
                if any(
                    w.overlaps(v) for w in ia.writes for v in ib.reads + ib.writes
                ) or any(r.overlaps(w) for r in ia.reads for w in ib.writes):
                    out[b].add(a)
        return out

    def order(self) -> dict:
        """Each placed coordinate's items, in sequence order."""
        out: dict = {}
        for i, it in enumerate(self.items):
            out.setdefault(it.at, []).append(i)
        return out

    def packages(self) -> list[list[int]]:
        """Item indices by package, packages in ascending order."""
        out: dict = {}
        for i, it in enumerate(self.items):
            out.setdefault(it.package, []).append(i)
        return [out[k] for k in sorted(out)]

    def check(self, units: dict | None = None) -> list[str]:
        """Problems, empty when the schedule may be lowered. `units` maps each
        coordinate to its unit type (`kohakuaccel.package.units.unit_types`)."""
        problems = []
        held = set(map(id, self.buffers))
        for i, it in enumerate(self.items):
            name = f"item {i} ({it.kind})"
            for v in it.reads + it.writes:
                if id(v.buffer) not in held:
                    problems.append(
                        f"{name} names {v.buffer.name}, not in the schedule"
                    )
                if v.offset < 0 or v.end > v.buffer.nbytes:
                    problems.append(
                        f"{name}: view [{v.offset}, {v.end}) is outside "
                        f"{v.buffer.name}'s {v.buffer.nbytes} bytes"
                    )
                local = v.buffer.local
                if local is not None and it.at is not None and tuple(local) != it.at:
                    problems.append(
                        f"{name} at {it.at} touches {local}'s {v.buffer.name}"
                    )
            if it.unit != "mover":
                if it.at is None:
                    problems.append(f"{name} is not placed")
                elif units is not None and units.get(it.at) != it.unit:
                    problems.append(f"{name} wants a {it.unit} and {it.at} is not one")
        for b, ds in enumerate(self.deps()):
            for a in ds:
                if self.items[a].package > self.items[b].package:
                    problems.append(
                        f"item {b} depends on item {a} of a later package "
                        f"({self.items[a].package} after {self.items[b].package})"
                    )
        mem = sorted(
            (b.base, b.base + b.nbytes, b.name)
            for b in self.buffers
            if b.space == "mem" and b.base is not None
        )
        reach, owner = None, None
        for start, end, name in mem:
            if reach is not None and start < reach:
                problems.append(f"buffers {owner} and {name} overlap in memory")
            if reach is None or end > reach:
                reach, owner = end, name
        return problems

    def allocate(self, alloc) -> None:
        """Give every memory buffer without an address one: ``alloc(nbytes)``
        returns a base."""
        for b in self.buffers:
            if b.space == "mem" and b.base is None:
                b.base = alloc(b.nbytes)

    def verify(self, units: dict | None = None) -> None:
        problems = self.check(units)
        if problems:
            raise ScheduleError("; ".join(problems))
