"""L2, the schedule: one dispatch per task, bound to a unit and a round.

A task must be dispatchable and countable: a known number of flits and a known
number of completion signals. `payload` is opaque to this level.
"""

from dataclasses import dataclass, field
from enum import Enum

from kohakuaccel.ir.base import Level, Node

Coord = tuple[int, int]


class Policy(Enum):
    """Which units a task may be placed on."""

    FREE = "free"
    PINNED = "pinned"
    AFFINE = "affine"


@dataclass(frozen=True)
class Region:
    """A byte range in the memory of one mesh.

    Coalescing groups on equality, so two tasks reading the same operand must
    describe it identically; overlapping-but-unequal regions are never merged.

    `mesh` is part of the address. Each mesh has its own memory, so the same
    `base` on two meshes names two different bytes -- which is why regions on
    different meshes never overlap and never coalesce.
    """

    base: int
    nbytes: int
    mesh: int = 0

    @property
    def end(self) -> int:
        return self.base + self.nbytes

    def overlaps(self, other: "Region") -> bool:
        if self.mesh != other.mesh:
            return False
        return self.base < other.end and other.base < self.end


@dataclass
class Task(Node):
    """One dispatch to one unit of type `unit_type`.

    Round packing and completion accounting are computed from `flits` and
    `signals`; a backend that declares them wrong produces a program that hangs.
    """

    unit_type: str = ""
    payload: object = None
    reads: tuple[Region, ...] = ()
    writes: tuple[Region, ...] = ()
    flits: int = 1
    signals: int = 1
    policy: Policy = Policy.FREE
    coord: Coord | None = None
    cost: float = 1.0
    #: Which mesh this task RUNS on; None infers it from the regions. Placement
    #: may not override it -- a task cannot run where its operands are not.
    mesh: int | None = None


@dataclass
class Round:
    """Tasks dispatched together, with a barrier after."""

    tasks: list[int] = field(default_factory=list)
    credits: int = 0

    def flits(self, schedule: "ScheduleIR") -> int:
        return sum(schedule[i].flits for i in self.tasks)


class ScheduleIR(Level):
    """Tasks, where they go, and which round they go in.

    `placement`, `meshes` and `rounds` start empty: they are filled by the
    framework's own passes, and :meth:`check` reports a level that reached
    emission without them. `placement[i]` is a coordinate WITHIN `meshes[i]`, so
    neither identifies a unit on its own once a machine carries several meshes.
    """

    name = "L2 schedule"

    def __init__(self) -> None:
        super().__init__()
        self.placement: list[Coord | None] = []
        self.meshes: list[int | None] = []
        self.rounds: list[Round] = []

    def task(self, unit_type: str, payload=None, **kw) -> int:
        """Append a task and return its index."""
        i = self.add(
            Task(
                kind=kw.pop("kind", unit_type),
                unit_type=unit_type,
                payload=payload,
                **kw,
            )
        )
        self.placement.append(self.nodes[i].coord)
        self.meshes.append(self.nodes[i].mesh)
        return i

    def infer_edges(self) -> int:
        """Add the edges implied by write-then-read or write-then-write overlap."""
        before = len(self.edges)
        for i, producer in enumerate(self.nodes):
            if not producer.writes:
                continue
            for j in range(i + 1, len(self.nodes)):
                consumer = self.nodes[j]
                touched = consumer.reads + consumer.writes
                if any(w.overlaps(r) for w in producer.writes for r in touched):
                    self.depends(i, j)
        return len(self.edges) - before

    def check(self) -> list[str]:
        problems = []
        for i, task in enumerate(self.nodes):
            if not task.unit_type:
                problems.append(f"task {task.described(i)} names no unit type")
            if task.flits < 1:
                problems.append(f"task {task.described(i)} occupies {task.flits} flits")
            if task.policy is Policy.PINNED and task.coord is None:
                problems.append(f"task {task.described(i)} is pinned to nowhere")
        if self.placement and len(self.placement) != len(self.nodes):
            problems.append(
                f"{len(self.placement)} placements for {len(self.nodes)} tasks"
            )
        if self.meshes and len(self.meshes) != len(self.nodes):
            problems.append(f"{len(self.meshes)} meshes for {len(self.nodes)} tasks")
        scheduled = [i for r in self.rounds for i in r.tasks]
        if self.rounds and sorted(scheduled) != list(range(len(self.nodes))):
            missing = sorted(set(range(len(self.nodes))) - set(scheduled))
            problems.append(f"tasks in no round: {missing}")
        if len(scheduled) != len(set(scheduled)):
            problems.append("a task appears in more than one round")
        return problems

    def describe(self, index: int, node: Task) -> str:
        where = self.placement[index] if index < len(self.placement) else None
        mesh = self.meshes[index] if index < len(self.meshes) else None
        at = f" @{where}" if where else ""
        on = f" mesh_{mesh}" if mesh is not None else ""
        return f"{node.described(index)} [{node.unit_type}]{on}{at} {node.flits}f"
