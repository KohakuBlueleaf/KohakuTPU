"""A card that runs on the L2 value simulator: a kernel compiles to its L2 and
stops there, and a run is that L2 on host memory. No hardware, no timing, no
lower stage: forms, memory and replay are what a run here tests.

    dev = Device(kind="double", board="multimesh_v9t8")
"""

import json

from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.stack import rs
from kohakutpu.stack.rs import _rs


class DoubleCard(OfflineCard):
    """A logged run keeps its kernel and physical bindings, what a replay runs."""

    def compile_graph(self, source: str) -> dict:
        """A compile's output with the L2 alone: the runtime asks an L1 body
        only for scratch parameters, and an L2 keeps its scratch to itself."""
        name = json.loads(source)["name"]
        return {
            "name": name,
            "l2": _rs.l2_of(source, self.machine_json, rs._MUTANT.get()),
            "l1": f"fn {name}.l1()\n",
            "verdict": {"refused": False, "findings": []},
            "packages": {},
            "scratch": [],
            "bound": None,
            "f001": {"line_ports": [], "split": 0},
        }

    def run_compiled(self, compiled, binds: dict) -> dict:
        if self.log is not None:
            self.log.append(("run", {}, compiled, binds))
        self.runs += 1
        self._simulate(compiled, binds)
        return {"cycles": 0, "steps": [], "phases": {}}

    def _simulate(self, compiled, binds: dict) -> None:
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
