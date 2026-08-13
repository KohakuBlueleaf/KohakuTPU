"""Running passes in order, verifying the level after each one by default."""

import abc
import time
from dataclasses import dataclass, field

from kohakuaccel.ir.base import IRError, Level


class Pass(abc.ABC):
    """One transformation of one level."""

    name: str = "pass"

    @abc.abstractmethod
    def run(self, level: Level, ctx: "Context") -> Level: ...


@dataclass
class Context:
    """What passes share: the machine, the backend, and `notes`.

    `notes` is where a pass leaves something a later pass or a report wants.
    """

    machine: object = None
    backend: object = None
    notes: dict = field(default_factory=dict)

    def note(self, key: str, value) -> None:
        self.notes[key] = value


@dataclass
class PassRecord:
    name: str
    seconds: float
    nodes: int


class PassManager:
    """An ordered pipeline over one or more levels."""

    def __init__(self, passes=(), verify: bool = True) -> None:
        self.passes: list[Pass] = list(passes)
        self.verify = verify
        self.history: list[PassRecord] = []

    def add(self, p: Pass) -> "PassManager":
        self.passes.append(p)
        return self

    def run(self, level: Level, ctx: Context) -> Level:
        """Run every pass in order, returning the final level.

        Raises :class:`IRError` naming the pass that produced a malformed level.
        """
        self.history.clear()
        for p in self.passes:
            started = time.perf_counter()
            level = p.run(level, ctx)
            if self.verify:
                try:
                    level.verify()
                except IRError as exc:
                    raise IRError(f"after pass {p.name!r}: {exc}") from exc
            self.history.append(
                PassRecord(p.name, time.perf_counter() - started, len(level))
            )
        return level

    def report(self) -> str:
        lines = ["pass                       nodes      ms"]
        for r in self.history:
            lines.append(f"{r.name:<24} {r.nodes:>6}  {r.seconds * 1e3:>6.1f}")
        return "\n".join(lines)
