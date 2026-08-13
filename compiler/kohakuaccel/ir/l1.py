"""L1, the program: encoded instruction flits, per unit, in dispatch order.

The last level before an artifact. It records which staging slots hold each
program, which unit it goes to, and how many completions to expect back.
"""

from dataclasses import dataclass

from kohakuaccel.ir.base import Level, Node

Coord = tuple[int, int]


@dataclass
class UnitProgram(Node):
    """The flits staged for one unit in one round.

    `base` is the first staging slot and `flits` are the words themselves, so
    ``base + len(flits)`` is where the next program may start.
    """

    coord: Coord = (0, 0)
    base: int = 0
    flits: tuple[int, ...] = ()
    signals: int = 0
    round: int = 0


class ProgramIR(Level):
    """Every unit program, plus the staging image they were laid into."""

    name = "L1 program"

    def __init__(self, machine: str = "generic") -> None:
        super().__init__()
        self.machine = machine
        self.staging: list[int] = []

    def stage(self, coord: Coord, flits, signals: int, round: int, label="") -> int:
        """Lay `flits` into the next free staging slots and record the program."""
        base = len(self.staging)
        words = tuple(flits)
        self.staging.extend(words)
        return self.add(
            UnitProgram(
                kind="program",
                label=label,
                coord=coord,
                base=base,
                flits=words,
                signals=signals,
                round=round,
            )
        )

    def rounds(self) -> int:
        return max((p.round for p in self.nodes), default=-1) + 1

    def check(self) -> list[str]:
        problems = []
        for i, prog in enumerate(self.nodes):
            if not prog.flits:
                problems.append(f"program {prog.described(i)} stages no flits")
            if prog.signals < 1:
                problems.append(
                    f"program {prog.described(i)} expects {prog.signals} signals, "
                    f"so nothing will ever release the wait on it"
                )
            got = tuple(self.staging[prog.base : prog.base + len(prog.flits)])
            if got != prog.flits:
                problems.append(
                    f"program {prog.described(i)} disagrees with the staging image "
                    f"at slot {prog.base}"
                )
        return problems

    def describe(self, index: int, node: UnitProgram) -> str:
        return (
            f"r{node.round} @{node.coord} slots {node.base}.."
            f"{node.base + len(node.flits) - 1} ({node.signals} sig)"
        )
