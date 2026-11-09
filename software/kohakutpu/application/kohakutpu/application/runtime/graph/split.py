"""Retain launch lifetimes while composing one address-bound node program."""

import json
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from types import MappingProxyType
from typing import Any

from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.graph import backend as graph_backend
from kohakutpu.application.runtime.graph.bound import bind
from kohakutpu.application.runtime.graph.native import names, renamed
from kohakutpu.application.runtime.os.graph import PreparedMultiGraph
from kohakutpu.application.runtime.split import (
    BoundGraphArtifact,
    PreparedFirmwareGraph,
)
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
    artifact: BoundGraphArtifact | None = None
    scratch: list[int] = field(default_factory=list)
    backends: list[Any] = field(default_factory=list)
    environment: dict | None = None
    scratch_limits: dict | None = None
    bound_bindings: dict | None = None
    bound_references: list = field(default_factory=list)
    proof_epoch: int | None = None

    def submit(self):
        if self.closed:
            raise RuntimeError("prepared split graph is closed")
        self.bind_artifact()
        if self.device.graph_backend is not None:
            return graph_backend.submit(self)
        return self.device._submitted(
            self.compiled, self.bound_bindings, "graph", None, pinned=True
        )

    def bind_artifact(self) -> BoundGraphArtifact:
        if self.closed:
            raise RuntimeError("prepared split graph is closed")
        if self.artifact is not None and self.proof_epoch != self.device.table_epoch:
            # A constant/address-table write invalidates the memory proof, not
            # the pinned alias layout. Retire adapters before replacing bytes.
            self.device.drain()
            for backend in self.backends:
                backend.close()
            self.backends.clear()
            for arena, address in self.bound_references:
                self.references.remove((arena, address))
                arena.unretain(address)
            self.bound_references.clear()
            self.artifact = self.bound_bindings = None
        if self.artifact is None:
            # This cache owns both the artifact and its validity stamp. Do not
            # depend on bind() mutating the stamp as an undocumented side effect.
            epoch = self.device.table_epoch
            self.artifact = bind(self)
            self.proof_epoch = epoch
        return self.artifact

    def prepare_firmware(self, queues, leases, **options):
        artifact = self.bind_artifact()
        backend = PreparedFirmwareGraph(
            artifact, queues, leases, self.references, **options
        )
        self.backends.append(backend)
        return backend

    def prepare_kaos(self, residents, units, leases, **options):
        artifact = self.bind_artifact()
        backend = PreparedMultiGraph(
            artifact, residents, units, leases, references=self.references, **options
        )
        self.backends.append(backend)
        return backend

    def close(self):
        if not self.closed:
            # Borrowed scratch may still be in use by an ordinary submission.
            self.device.drain()
            for backend in self.backends:
                backend.close()
            for arena, address in self.references:
                arena.unretain(address)
            self.references.clear()
            for address in self.scratch:
                self.device.free(address)
            self.scratch.clear()
            self.closed = True


class SplitRunner:
    """Compile launches separately, then compose quiescent scratch lifetimes.

    External aliases are resolved under pins before lowering. Launch-local
    placement and legalization stay identical to eager; the compiler joins
    launches on the device before reusing their pooled scratch.
    """

    def __init__(self, device, *, pinned=False):
        self.device = device
        self.pinned = pinned

    def prepare(self, packets, dependencies):
        if not packets:
            raise ValueError("a split graph requires at least one launch")
        if len(dependencies) != len(packets):
            raise ValueError("one dependency list is required per launch")
        first = packets[0].graph
        graph = {key: first[key] for key in ("format", "version", "target", "machine")}
        graph.update(name="captured_split", consts={}, ports=[], scratch=[], body=[])
        bindings, launches = {}, []
        order = TopologicalSorter(dict(enumerate(dependencies))).static_order()
        for index in order:
            if not 0 <= index < len(packets):
                raise ValueError("dependency names a launch outside the graph")
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
            launches.append(json.dumps(part))
        references = self.device._retain(bindings.values(), pinned=self.pinned)
        try:
            physical = {
                name: self.device.mmu.resolve(at)[1] for name, at in bindings.items()
            }
            with rs.compiler_owner(self.device):
                output = rs._rs.compile_split_graph(
                    json.dumps(graph),
                    rs.machine_json(self.device.machine),
                    physical,
                    zero_sign=rs._ZERO_SIGN.get(),
                    launches=launches,
                    default_node=self.device._node_of(None),
                )
            compiled = generated.from_output(self.device, graph, output)
            # Single-launch capture retains every legacy checker verdict.
            compiled.compositional = len(launches) > 1
            return PreparedSplitGraph(
                self.device,
                compiled,
                MappingProxyType(bindings),
                tuple(dependencies),
                tuple(output["events"]),
                references,
                environment=physical,
            )
        except BaseException:
            for arena, address in references:
                arena.unretain(address)
            raise
