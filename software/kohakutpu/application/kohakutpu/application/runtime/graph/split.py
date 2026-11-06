"""Compose bound semantic launches before node ordering and slot allocation."""

import json
from dataclasses import dataclass
from graphlib import TopologicalSorter
from types import MappingProxyType
from typing import Any

from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.graph.native import names, renamed
from kohakutpu.stack import rs


@dataclass
class PreparedSplitGraph:
    device: Any
    compiled: Any
    bindings: MappingProxyType
    dependencies: tuple
    events: tuple
    references: list
    closed: bool = False

    def submit(self):
        if self.closed:
            raise RuntimeError("prepared split graph is closed")
        return self.device.run(self.compiled, self.bindings, kind="graph")

    def close(self):
        if not self.closed:
            for arena, address in self.references:
                arena.unretain(address)
            self.references.clear()
            self.closed = True


class SplitRunner:
    """A graph runner whose packets are NativeLaunch objects with virtual bindings.

    External aliases are resolved once under allocation pins. The whole graph is
    lowered and scheduled together; replay uses ordinary device submission.
    """

    def __init__(self, device):
        self.device = device

    def prepare(self, packets, dependencies):
        if not packets:
            raise ValueError("a split graph requires at least one launch")
        first = packets[0].graph
        graph = {key: first[key] for key in ("format", "version", "target", "machine")}
        graph.update(name="captured_split", consts={}, ports=[], scratch=[], body=[])
        bindings = {}
        order = TopologicalSorter(dict(enumerate(dependencies))).static_order()
        for index in order:
            packet = packets[index]
            mapping = names(packet.graph, f"call{index}_")
            part = renamed(packet.graph, mapping)
            graph["consts"].update(
                {f"call{index}_{name}": value for name, value in part["consts"].items()}
            )
            graph["ports"].extend(part["ports"])
            graph["scratch"].extend(part["scratch"])
            graph["body"].extend(part["body"])
            bindings.update({mapping[name]: at for name, at in packet.bindings.items()})
        references = self.device._retain(bindings.values())
        try:
            physical = {
                name: self.device.mmu.resolve(at)[1] for name, at in bindings.items()
            }
            output = rs._rs.compile_split_graph(
                json.dumps(graph),
                rs.machine_json(self.device.machine),
                physical,
                zero_sign=rs._ZERO_SIGN.get(),
            )
            compiled = generated.from_output(self.device, graph, output)
            return PreparedSplitGraph(
                self.device,
                compiled,
                MappingProxyType(bindings),
                tuple(dependencies),
                tuple(output["events"]),
                references,
            )
        except BaseException:
            for arena, address in references:
                arena.unretain(address)
            raise
