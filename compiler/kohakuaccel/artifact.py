"""What a compiler produces and a driver executes.

An artifact holds staged instruction flits and a schedule of seeds, kicks,
awaits and barriers, and no register addresses -- so anything holding the
register map can execute one: the driver, a simulator, a bring-up script.
"""

from dataclasses import dataclass, field

from kohakuaccel.machinespec import Coord


@dataclass(frozen=True)
class SeedCredits:
    """Set the dispatch credit for a whole round. Once, before any kick."""

    n: int


@dataclass(frozen=True)
class Kick:
    """Launch `nflits` staged flits at `coord`, starting at staging slot `base`."""

    coord: Coord
    base: int
    nflits: int


@dataclass(frozen=True)
class Await:
    """Block until `coord` has produced `count` more completions this round."""

    coord: Coord
    count: int


@dataclass(frozen=True)
class Barrier:
    """Everything kicked so far has completed before anything after is staged."""


Step = SeedCredits | Kick | Await | Barrier


@dataclass
class Artifact:
    """A compiled program: the payloads to stage, and what to do with them.

    `flits` holds 256-bit INSTRUCTION PAYLOADS indexed by staging slot, not
    whole flits; the routing header is the driver's. `steps` is executed in
    order. `meta` carries whatever the frontend wants to record.
    """

    machine: str = "generic"
    flits: list[int] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def rounds(self) -> int:
        """How many barriers separate this program, plus one."""
        return sum(1 for s in self.steps if isinstance(s, Barrier)) + 1

    @property
    def kicks(self) -> int:
        return sum(1 for s in self.steps if isinstance(s, Kick))

    def summary(self) -> str:
        return (
            f"{self.machine}: {len(self.flits)} flits, {self.kicks} kicks, "
            f"{self.rounds} rounds, {len(self.steps)} steps"
        )

    def to_dict(self) -> dict:
        """The serialised form: plain data with tagged steps.

        Raises :class:`TypeError` for a step this cannot tag.
        """
        steps = []
        for s in self.steps:
            if isinstance(s, SeedCredits):
                steps.append(["seed", s.n])
            elif isinstance(s, Kick):
                steps.append(["kick", s.coord[0], s.coord[1], s.base, s.nflits])
            elif isinstance(s, Await):
                steps.append(["await", s.coord[0], s.coord[1], s.count])
            elif isinstance(s, Barrier):
                steps.append(["barrier"])
            else:
                raise TypeError(f"unknown step {s!r}")
        return {
            "machine": self.machine,
            "flits": list(self.flits),
            "steps": steps,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Artifact":
        """Rebuild an artifact from :meth:`to_dict`."""
        tags = {
            "seed": lambda a: SeedCredits(a[0]),
            "kick": lambda a: Kick((a[0], a[1]), a[2], a[3]),
            "await": lambda a: Await((a[0], a[1]), a[2]),
            "barrier": lambda a: Barrier(),
        }
        steps = []
        for tag, *args in data["steps"]:
            if tag not in tags:
                raise ValueError(f"unknown step tag {tag!r}")
            steps.append(tags[tag](args))
        return cls(
            machine=data.get("machine", "generic"),
            flits=list(data.get("flits", [])),
            steps=steps,
            meta=dict(data.get("meta", {})),
        )
