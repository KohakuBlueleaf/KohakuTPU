"""A card on the behaviour model: each node's packages by the firmware's step
semantics (`Interpreter`) over one memory, the moves by the mover's model,
and every unit a process of its own (`vector.VectorCore`, `cluster.Cluster`)
kept across packages, as a unit keeps its instruction memory, descriptors and
counters. No cycles: a unit runs whenever what it waits for is there (a
COUNT burst landed, a credit received), the units in turn until none can.

    card = Card(memory, machine)
    card.run(pkg, bindings)

A unit of another kind, or a step no model holds, raises
:class:`Unmodelled`."""

from kohakuaccel.simulation.package import Interpreter, Result
from kohakutpu.simulation.behave.cluster import Cluster
from kohakutpu.simulation.behave.mover import Mover
from kohakutpu.simulation.behave.vector import Unmodelled, VectorCore

#: CU_SIGNAL DATA_RECEIVED, a sink's answer to a burst asking for one.
DATA_RECEIVED = 0x03


def kind_of(unit) -> str:
    """A package unit's kind, its two-letter type ("VC", "MG", ...)."""
    return unit.type.to_bytes(2, "big").decode("ascii", "replace")


class Card:
    """`memory` (``read_block`` / ``write_block``) and the models of every unit
    a package names, made at first use; `machine` gives a vector core's
    register count."""

    def __init__(self, memory, machine=None) -> None:
        self.memory = memory
        params = machine.params.get("VC", {}) if machine is not None else {}
        self.regs = params.get("regs", 32)
        self.units: dict = {}  # (mesh, x, y) -> model
        self.mover = Mover(memory)
        #: signals for the node: (mesh, x, y, code, arg)
        self.signals: list = []

    def model(self, mesh: int, unit):
        key = (mesh, unit.x, unit.y)
        got = self.units.get(key)
        if got is None:
            kind = kind_of(unit)
            if kind == "VC":
                got = VectorCore(self, key, self.memory, self.regs)
            elif kind == "MG":
                got = Cluster(self, key, self.memory)
            else:
                raise Unmodelled(f"a {kind} unit at ({unit.x}, {unit.y}) has no model")
            self.units[key] = got
        return got

    # ------------------------------------------------------------ routes
    def burst(self, mesh, to, buf, off, data, count, signal, ack) -> None:
        """A CU_DATA burst into the unit at `to`; with `signal`, the sink
        answers DATA_RECEIVED to `ack` (zero: the sender, which drops it)."""
        sink = self.units.get((mesh, *to))
        if sink is None:
            raise Unmodelled(f"a burst to ({to[0]}, {to[1]}), a unit with no model yet")
        sink.land(buf, off, data, count)
        if signal and ack != (0, 0):
            self.signals.append((mesh, *to, DATA_RECEIVED, 0))

    def credit(self, mesh, to, n: int) -> None:
        sink = self.units.get((mesh, *to))
        if sink is None:
            raise Unmodelled(
                f"a credit to ({to[0]}, {to[1]}), a unit with no model yet"
            )
        sink.credit(n)

    def settle(self) -> None:
        """Every unit run, each every round, until none progresses."""
        while True:
            moved = [u.step() for u in self.units.values()]
            if not any(moved):
                return

    # --------------------------------------------------------------- run
    def run(self, pkg, bindings, mesh: int = 0) -> Result:
        """Run `pkg` (`bindings` its buffers' addresses) as node `mesh`'s;
        raises RuntimeError naming a failed package's status."""
        for u in pkg.units:
            self.model(mesh, u)

        def moves(writes) -> None:
            for reg, value in writes:
                self.mover.write(reg, value)

        def store(addr: int, value: int) -> None:
            self.memory.write_block(addr, value.to_bytes(8, "little"))

        interp = Interpreter(
            _Node(self, mesh), mover=moves, memory=self.memory.read_block, store=store
        )
        res = interp.run(pkg, list(bindings), max_polls=4)
        if res.status:
            stuck = {
                k: f"{len(u.words)} words queued, {'running' if u.task else 'idle'}"
                for k, u in self.units.items()
                if u.task is not None or u.words
            }
            raise RuntimeError(
                f"package failed on the behaviour model: status {res.status:#x} "
                f"detail {res.detail} at step {res.step}; units waiting: {stuck}"
            )
        return res


class _Node:
    """The mailbox node `mesh`'s interpreter sends through: a word to its
    unit's queue; a drain runs the units, then hands over what they raised."""

    def __init__(self, card: Card, mesh: int) -> None:
        self.card, self.mesh = card, mesh

    def send(self, x: int, y: int, payload: int) -> None:
        self.card.units[(self.mesh, x, y)].deliver(payload)

    def drain(self) -> list:
        card = self.card
        card.settle()
        out = []
        for (mesh, x, y), u in card.units.items():
            if mesh == self.mesh:
                out += [(x, y, c, a) for c, a in u.out]
                u.out.clear()
        mine = [s for s in card.signals if s[0] == self.mesh]
        card.signals = [s for s in card.signals if s[0] != self.mesh]
        return out + [s[1:] for s in mine]


def run(pkg, bindings, memory) -> Result:
    """One package on a fresh card over `memory`."""
    return Card(memory).run(pkg, bindings)


__all__ = ["Card", "Unmodelled", "kind_of", "run"]
