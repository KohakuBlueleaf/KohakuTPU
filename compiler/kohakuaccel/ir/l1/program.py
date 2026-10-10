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
    #: ``("send", coord, words, ops)`` / ``("mark", coord, token)`` /
    #: ``("wait", coord, token)`` / ``("barrier",)`` / ``("move", writes, ops)``
    steps: list = field(default_factory=list)
    #: unit type -> ``f(words, state, coord) -> words``; see the module docstring.
    lowerings: ClassVar[dict] = {}

    def send(self, coord, *ops) -> "Program":
        words = [w for op in ops for w in op.flits()]
        if words:
            self.steps.append(("send", tuple(coord), words, tuple(ops)))
        return self

    def mark(self, coord) -> int:
        """A point in `coord`'s stream: everything sent to it so far. Returns
        the token a `wait` names."""
        token = sum(1 for s in self.steps if s[0] == "mark")
        self.steps.append(("mark", tuple(coord), token))
        return token

    def wait(self, coord, token: int | None = None) -> "Program":
        """Hold the node until `coord` has completed everything sent to it --
        or, given a `mark` token of that unit, everything sent up to the mark,
        whatever was sent after it."""
        if token is not None:
            at = next(
                (s[1] for s in self.steps if s[0] == "mark" and s[2] == token), None
            )
            if at != tuple(coord):
                raise ValueError(f"token {token} marks {at}, not {tuple(coord)}")
        self.steps.append(("wait", tuple(coord), token))
        return self

    def barrier(self) -> "Program":
        self.steps.append(("barrier",))
        return self

    def move(self, *ops) -> "Program":
        """A mover step: ops with ``writes()`` (register writes), a list of
        them, or one list of raw ``(register, value)`` writes."""
        if len(ops) == 1 and isinstance(ops[0], list):
            if all(isinstance(w, tuple) for w in ops[0]):
                self.steps.append(("move", list(ops[0]), ()))
                return self
            ops = tuple(ops[0])
        writes = [w for op in ops for w in op.writes()]
        self.steps.append(("move", writes, tuple(ops)))
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
        # Words dispatched to and awaited from each unit, cumulative: an AWAIT
        # raises a unit's expected count (package-format.md), so a wait up to a
        # mark is the difference.
        sent: dict = {}
        awaited: dict = {}
        marks: dict = {}
        # Sends between two sync points are unordered across units, so they go
        # out as few dispatches as the node allows: a fetch-port request costs
        # ~1k node cycles whatever its length (MEASURED on card_v9_1n). The node
        # sends a fetched step whole, waiting on that unit's credit, before the
        # next step, so a stream past its unit's credit is cut at the credit and
        # the units' pieces interleave: no unit waits idle behind another's
        # credit (MEASURED: 16 vector epilogues, 973 words a core, 411k cycles as
        # one step a core, 310k split by package). A stream within the credit
        # stays one step: cut at 255 anyway, add 262144 ran 93.9k -> 107.8k.
        pending: dict = {}
        piece = self.machine.inst_depth

        def flush(only=None) -> None:
            ready = []
            for u in [only] if only is not None else list(pending):
                words = pending.pop(u, [])
                coord = (b.units[u].x, b.units[u].y)
                lower = self.lowerings.get(types.get(coord))
                if resident is not None and lower is not None:
                    words = lower(words, resident, coord)
                if words:
                    ready.append((u, words))
                    sent[u] = sent.get(u, 0) + len(words)
            for k in range(0, max((len(w) for _, w in ready), default=0), piece):
                for u, words in ready:
                    if words[k : k + piece]:
                        b.dispatch(u, words[k : k + piece])

        def await_to(u, upto: int) -> None:
            if upto > awaited.get(u, 0):
                b.await_(u, upto - awaited.get(u, 0))
                awaited[u] = upto

        for step in self.steps:
            match step[0]:
                case "send":
                    pending.setdefault(b.unit(step[1]), []).extend(step[2])
                case "mark":
                    # The unit's words up to here go out now, so the count a
                    # wait names is what the unit is actually sent.
                    u = b.unit(step[1])
                    flush(u)
                    marks[step[2]] = sent.get(u, 0)
                case "wait":
                    u = b.unit(step[1])
                    flush()
                    await_to(u, sent.get(u, 0) if step[2] is None else marks[step[2]])
                case "barrier":
                    flush()
                    for u in sorted(sent):
                        await_to(u, sent[u])
                    b.barrier()
                case "move":
                    flush()
                    b.mover(step[1])
                    b.barrier()
        flush()
        for u in sorted(sent):
            await_to(u, sent[u])
        b.barrier()
        return b


__all__ = ["Program", "machine_units"]
