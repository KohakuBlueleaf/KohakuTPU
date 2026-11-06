"""A device double backed by the authoritative simulators.
Graph kernels stop at L2; authoritative L1 motifs compile through the package
boundary and execute on the L0 simulator. Forms, memory and replay are shared.

    dev = Device(kind="double", board="multimesh_v9t8")
"""

import json
from dataclasses import dataclass, field

from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import rs
from kohakutpu.stack.rs import _rs


@dataclass
class PendingSimulation:
    compiled: object
    bindings: dict
    result: dict = field(default_factory=dict)
    failure: ValueError | None = None
    order: list = field(default_factory=list)
    completed: bool = False
    collected: bool = False


class DoubleCard(OfflineCard):
    """A logged run keeps its kernel and physical bindings, what a replay runs."""

    def compile_graph(self, source: str) -> dict:
        """A compile's output with the L2 alone: the runtime asks an L1 body
        only for scratch parameters, and an L2 keeps its scratch to itself."""
        graph = json.loads(source)
        name = graph["name"]
        if "executable_l1" in graph:
            return dict(rs.compile(source, name, self.machine))
        return {
            "name": name,
            "l2": _rs.l2_of(source, self.machine_json, rs._MUTANT.get()),
            "l1": f"fn {name}.l1()\n",
            "verdict": {"refused": False, "findings": []},
            "packages": {},
            "scratch": [],
            "bound": None,
            "f001": {"line_ports": [], "split": 0},
            "f016": {"burst_ports": []},
        }

    def submit_launch(self, program, compiled, bindings):
        """Publish every share, then simulate all node scopes together."""
        pending = PendingSimulation(compiled, bindings())
        nodes = tuple(program) if isinstance(program, dict) else (self.nodes[0],)
        pending.order.extend(("publish", node) for node in nodes)
        if self.log is not None:
            self.log.append(("run", {}, compiled, pending.bindings))
        self._publishing()
        try:
            self._simulate(pending.compiled, pending.bindings)
        except ValueError as error:
            pending.failure = error
        else:
            self.runs += 1
            pending.result = {"cycles": 0, "steps": [], "phases": {}}
        pending.completed = True
        return pending

    def collect(self, pending):
        """Return the terminal result without repeating simulation effects."""
        if not pending.collected:
            nodes = [node for action, node in pending.order if action == "publish"]
            pending.order.extend(("collect", node) for node in nodes)
            pending.collected = True
        if pending.failure is not None:
            raise pending.failure
        return pending.result

    def run_compiled(self, compiled, binds: dict) -> dict:
        return self.collect(self.submit_launch(None, compiled, lambda: dict(binds)))

    def _simulate(self, compiled, binds: dict) -> None:
        if not compiled.l2_text:
            _rs.simulate_l1(
                compiled.l1.source,
                compiled.name,
                binds,
                self.mem.read_block,
                self.mem.write_block,
                self.machine_json,
            )
            return
        _rs.simulate_l2(
            compiled.l2_text,
            compiled.name,
            binds,
            self.mem.read_block,
            self.mem.write_block,
        )

    def replay(self, log: list) -> list:
        cycles = []
        for ev in log:
            cycles += super().replay([ev])
            if ev[0] == "run":
                self._simulate(*ev[2:])
        return cycles


__all__ = ["DoubleCard"]
