"""An L1 program: per-unit streams and the node's sync points, lowered to an L0
package (docs/spec/l1-program.md).

`send` queues words for a unit (any op with a ``flits()``); `wait` holds the
node until that unit has completed everything sent to it, one completion per
word; `barrier` until every unit has; `move` is a mover step. Nothing here
chooses anything: the order, the units and the words are the program's.

A project names, per unit type, a `lowerings` hook ``f(words, state, coord)``
that rewrites a unit's stream against what the unit already holds -- `state`
is the caller's dict, kept across packages, e.g. a vector core's resident
instruction memory.
"""

from dataclasses import dataclass, field
from typing import ClassVar

from kohakuaccel.package.build import PackageBuilder
from kohakuaccel.package.units import machine_units, unit_types


@dataclass
class Program:
    machine: object
    #: ``("send", coord, words)`` / ``("wait", coord)`` / ``("barrier",)`` / ``("move", writes)``
    steps: list = field(default_factory=list)
    #: unit type -> ``f(words, state, coord) -> words``; see the module docstring.
    lowerings: ClassVar[dict] = {}

    def send(self, coord, *ops) -> "Program":
        words = [w for op in ops for w in op.flits()]
        if words:
            self.steps.append(("send", tuple(coord), words))
        return self

    def wait(self, coord) -> "Program":
        self.steps.append(("wait", tuple(coord)))
        return self

    def barrier(self) -> "Program":
        self.steps.append(("barrier",))
        return self

    def move(self, writes) -> "Program":
        self.steps.append(("move", list(writes)))
        return self

    def units(self, kind: str) -> list:
        """The coordinates of every unit of `kind` on this machine."""
        types = unit_types(self.machine)
        return sorted(c for c, n in types.items() if n == kind)

    def build(self, fetch=None, resident=None) -> PackageBuilder:
        """The package. `fetch` is the memory port units stream their words
        from; `resident` is the state the `lowerings` hooks keep across
        packages (None: no hook runs, every word is sent)."""
        types = unit_types(self.machine)
        b = PackageBuilder(
            types=types,
            credit=self.machine.inst_depth,
            mesh=self.machine.default,
            fetch=fetch,
        )
        owed: dict = {}
        # Sends between two sync points are unordered across units, so each
        # unit's go out as ONE dispatch: a fetch-port request costs ~1k node
        # cycles whatever its length (MEASURED on card_v9_1n).
        pending: dict = {}

        def flush() -> None:
            for u, words in pending.items():
                coord = (b.units[u].x, b.units[u].y)
                lower = self.lowerings.get(types.get(coord))
                if resident is not None and lower is not None:
                    words = lower(words, resident, coord)
                if not words:
                    continue
                b.dispatch(u, words)
                owed[u] = owed.get(u, 0) + len(words)
            pending.clear()

        for step in self.steps:
            match step[0]:
                case "send":
                    pending.setdefault(b.unit(step[1]), []).extend(step[2])
                case "wait":
                    u = b.unit(step[1])
                    flush()
                    b.await_(u, owed.pop(u, 0))
                case "barrier":
                    flush()
                    for u, n in sorted(owed.items()):
                        b.await_(u, n)
                    owed.clear()
                    b.barrier()
                case "move":
                    flush()
                    b.mover(step[1])
                    b.barrier()
        flush()
        for u, n in sorted(owed.items()):
            b.await_(u, n)
        b.barrier()
        return b


__all__ = ["Program", "machine_units"]
