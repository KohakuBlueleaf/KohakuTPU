"""IR infrastructure every level shares.

Nodes with identity, a dependency relation, a topological order, a verifier and
a readable dump. A project defines what its nodes mean.
"""

import abc
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import ClassVar


class IRError(Exception):
    """A level is malformed. Raised by :meth:`Level.verify`."""


@dataclass
class Node:
    """One item at some level: an op, a task, a unit program.

    `kind` is the project's vocabulary and the framework never interprets it.
    `attrs` is whatever the project needs to carry; passes preserve it.
    """

    kind: str
    attrs: dict = field(default_factory=dict)
    label: str = ""

    def described(self, index: int) -> str:
        return self.label or f"{self.kind}#{index}"


class DepGraph:
    """A dependency relation over node indices, and the algorithms on it."""

    def __init__(self) -> None:
        self.edges: set[tuple[int, int]] = set()

    def depends(self, producer: int, consumer: int) -> None:
        """Require `producer` to complete before `consumer` begins."""
        if producer == consumer:
            raise IRError(f"node {producer} cannot depend on itself")
        self.edges.add((producer, consumer))

    def predecessors(self) -> dict[int, set[int]]:
        """For each node index, the indices that must precede it."""
        out: dict[int, set[int]] = {i: set() for i in range(self._count())}
        for producer, consumer in self.edges:
            out[consumer].add(producer)
        return out

    def order(self) -> list[int]:
        """A topological order. Raises :class:`IRError` on a cycle."""
        pending = {i: 0 for i in range(self._count())}
        after: dict[int, list[int]] = {i: [] for i in pending}
        for producer, consumer in self.edges:
            pending[consumer] += 1
            after[producer].append(consumer)

        ready = sorted(i for i, n in pending.items() if n == 0)
        out: list[int] = []
        while ready:
            i = ready.pop(0)
            out.append(i)
            for j in sorted(after[i]):
                pending[j] -= 1
                if pending[j] == 0:
                    ready.append(j)
        if len(out) != self._count():
            raise IRError(
                f"dependency cycle among nodes {sorted(set(pending) - set(out))}"
            )
        return out

    def depth(self) -> int:
        """The longest dependency chain."""
        best = {i: 1 for i in range(self._count())}
        for i in self.order():
            for producer, consumer in self.edges:
                if producer == i:
                    best[consumer] = max(best[consumer], best[i] + 1)
        return max(best.values(), default=0)

    def _count(self) -> int:
        raise NotImplementedError


class Level(DepGraph, abc.ABC):
    """One stage of the IR.

    Subclasses hold a list of nodes and say what makes them valid, in
    :meth:`check`.
    """

    name: ClassVar[str] = "level"

    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[Node] = []

    def add(self, node: Node) -> int:
        """Append `node` and return its index."""
        self.nodes.append(node)
        return len(self.nodes) - 1

    def _count(self) -> int:
        return len(self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self) -> Iterator[Node]:
        return iter(self.nodes)

    def __getitem__(self, i: int) -> Node:
        return self.nodes[i]

    @abc.abstractmethod
    def check(self) -> list[str]: ...

    def verify(self) -> None:
        """Raise :class:`IRError` listing everything wrong with this level."""
        problems = list(self.check())
        try:
            self.order()
        except IRError as exc:
            problems.append(str(exc))
        if problems:
            joined = "\n  ".join(problems)
            raise IRError(f"{self.name} is malformed:\n  {joined}")

    def pretty(self) -> str:
        """A dump, one line per node plus the dependency edges."""
        lines = [f"{self.name}: {len(self.nodes)} nodes, {len(self.edges)} edges"]
        for i, node in enumerate(self.nodes):
            lines.append(f"  [{i:3}] {self.describe(i, node)}")
        for producer, consumer in sorted(self.edges):
            lines.append(f"        {producer} -> {consumer}")
        return "\n".join(lines)

    def describe(self, index: int, node: Node) -> str:
        """One line for `node`."""
        return node.described(index)
