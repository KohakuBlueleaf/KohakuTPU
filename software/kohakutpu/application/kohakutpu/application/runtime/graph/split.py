"""Plan retained launch parts before composing or compiling node programs."""

import json
from dataclasses import dataclass, field, replace
from graphlib import TopologicalSorter
from types import MappingProxyType
from typing import Any, TypedDict

from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.device.common import Submitted
from kohakutpu.application.runtime.graph import backend as graph_backend
from kohakutpu.application.runtime.graph.bound import bind
from kohakutpu.application.runtime.graph.limits import (
    CapacityExceeded,
    inspect_artifact,
)
from kohakutpu.application.runtime.graph.native import names, renamed
from kohakutpu.application.runtime.graph.part_plan import LaunchUsage, plan_parts
from kohakutpu.application.runtime.graph.parts import (
    BoundGraphSequence,
    bind_parts,
    bind_planned_parts,
    capacity_error,
)
from kohakutpu.application.runtime.os.graph import PreparedMultiGraph
from kohakutpu.application.runtime.os.sequence import PreparedMultiSequence
from kohakutpu.application.runtime.split import (
    BoundGraphArtifact,
    PreparedFirmwareGraph,
)
from kohakutpu.language.config import ENV
from kohakutpu.stack import rs


class _PartContext(TypedDict, total=False):
    part_nodes: list[int]
    part_first: int
    launch_count: int


@dataclass
class PreparedSplitGraph:
    device: Any
    compiled: Any
    bindings: MappingProxyType
    dependencies: tuple
    events: tuple
    references: list
    closed: bool = False
    artifact: BoundGraphArtifact | BoundGraphSequence | None = None
    scratch: list[int] = field(default_factory=list)
    backends: list[Any] = field(default_factory=list)
    environment: dict | None = None
    scratch_limits: dict | None = None
    bound_bindings: dict | None = None
    bound_references: list = field(default_factory=list)
    proof_epoch: int | None = None
    resource_usage: Any = None
    part_compiled: tuple = ()
    admission: dict | None = None
    planned_parts: tuple = ()
    part_intervals: tuple = ()
    part_bindings: tuple = ()
    launch_factory: Any = None
    part_plan: Any = None
    proof_work: dict | None = None

    def submit(self) -> Submitted:
        if self.closed:
            raise RuntimeError("prepared split graph is closed")
        self.bind_artifact()
        if self.device.graph_backend is not None or (
            isinstance(self.artifact, BoundGraphSequence)
            and getattr(self.device.card, "queues", None)
        ):
            return graph_backend.submit(self)
        if isinstance(self.artifact, BoundGraphSequence):
            submitted = None
            bindings = self.part_bindings or (self.bound_bindings,) * len(
                self.part_compiled
            )
            for compiled, bound in zip(self.part_compiled, bindings, strict=True):
                submitted = self.device._submitted(
                    compiled, bound, "graph", None, pinned=True
                )
                self.device.collect_through(submitted)
            if submitted is None:
                raise RuntimeError("bound sequence has no compiled parts")
            return submitted
        return self.device._submitted(
            self.compiled, self.bound_bindings, "graph", None, pinned=True
        )

    def bind_artifact(self) -> BoundGraphArtifact | BoundGraphSequence:
        if self.closed:
            raise RuntimeError("prepared split graph is closed")
        if self.artifact is not None and self.proof_epoch != self.device.table_epoch:
            self._discard_bound()
        artifact = self.artifact
        if artifact is None:
            epoch = self.device.table_epoch
            if self.planned_parts:
                artifact = bind_planned_parts(self, bind)
            else:
                try:
                    artifact = bind(self)
                except Exception as error:
                    if not getattr(
                        self.compiled, "compositional", False
                    ) or not capacity_error(error):
                        raise
                    if self.launch_factory is not None:
                        # Refine only this already planned interval.
                        artifact = bind_planned_parts(self, bind, error)
                    else:
                        artifact = bind_parts(self, bind, error)
            self.artifact = artifact
            self.proof_epoch = epoch
        return artifact

    def _discard_bound(self):
        self.device.drain()
        for backend in self.backends:
            backend.close()
        self.backends.clear()
        for arena, address in self.bound_references:
            self.references.remove((arena, address))
            arena.unretain(address)
        self.bound_references.clear()
        self.artifact = self.bound_bindings = None
        self.part_compiled = self.part_bindings = ()
        self.resource_usage = None
        self.proof_work = None
        # Child records describe bindings; they are not additional owners.
        for child in self.planned_parts:
            child.references.clear()
            child.bound_references = []
            child.bound_bindings = None

    def prepare_firmware(self, queues, leases, **options):
        backend = PreparedFirmwareGraph(
            self.bind_artifact(), queues, leases, self.references, **options
        )
        self.backends.append(backend)
        return backend

    def prepare_kaos(self, residents, units, leases, **options):
        admission = dict(residents=residents, units=units, **options)
        if self.artifact is not None:
            stages = self.artifact.stages
            try:
                for stage in stages:
                    inspect_artifact(stage, self.device.card.machine_json, **admission)
            except CapacityExceeded:
                self._discard_bound()
        self.admission = admission
        artifact = self.bind_artifact()
        if isinstance(artifact, BoundGraphSequence):
            backend = PreparedMultiSequence(
                artifact,
                residents,
                units,
                leases,
                references=self.references,
                **options,
            )
        else:
            backend = PreparedMultiGraph(
                artifact,
                residents,
                units,
                leases,
                references=self.references,
                **options,
            )
        self.backends.append(backend)
        return backend

    def close(self):
        if not self.closed:
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
    """Capacity-plan the launch list, then compose only each planned part.

    Captured physical launches carry pre-ENGINE eager-instance counts. Trace-only
    devices and newly fused/direct launches may have no eager package at all;
    obtain their individual package before planning, not a whole composition.
    """

    def __init__(self, device, *, pinned=False, max_part_launches=None):
        self.device = device
        self.pinned = pinned
        configured = ENV.get("KTPU_GRAPH_PART_LAUNCHES")
        self.max_part_launches = (
            int(configured)
            if max_part_launches is None and configured is not None
            else max_part_launches
        )
        if self.max_part_launches is not None and (
            type(self.max_part_launches) is not int or self.max_part_launches < 1
        ):
            raise ValueError("KTPU_GRAPH_PART_LAUNCHES must be a positive integer")

    def _package_usage(self, packages):
        if not packages or any(p.get("template_params") for p in packages.values()):
            return None
        result = {}
        home = self.device.home
        moved = list(packages) == [0]
        for node, package in packages.items():
            raw = package["bytes"]
            if moved and home is not None:
                node = int(home)
                raw = rs._rs.package_rehome(raw, self.device.card.machine_json, node)
            counted = LaunchUsage.read(raw)
            if counted is None:
                return None
            result[int(node if node is not None else self.device._node_of(None))] = (
                counted
            )
        return result

    def _usage(self, packet):
        if packet.package_usage is not None:
            return packet.package_usage
        source = json.dumps(packet.graph)
        compiled = self.device.kernel(source)
        counted = self._package_usage(compiled.packages)
        if counted is not None:
            return counted
        # DoubleCard.compile_graph deliberately stops at L2. Treating its empty
        # package map as "too large" split EVERY fitting region into singletons.
        # Class placeholders / ENGINE-only libraries likewise cannot be counted
        # as the exact source instance. Compile just this launch, without the
        # device's trace-only/class shortcut; normal disk compile caching applies.
        with rs.compiler_owner(self.device):
            exact = rs.compile(source, compiled.name, self.device.machine)
        return self._package_usage(exact["packages"])

    def prepare(self, packets, dependencies):
        if not packets:
            raise ValueError("a split graph requires at least one launch")
        if len(dependencies) != len(packets):
            raise ValueError("one dependency list is required per launch")
        order = tuple(TopologicalSorter(dict(enumerate(dependencies))).static_order())
        if len(order) != len(packets) or any(not 0 <= i < len(packets) for i in order):
            raise ValueError("dependency names a launch outside the graph")
        usages = tuple(self._usage(packets[i]) for i in order)
        fallback = (
            tuple(self.device.card.nodes) if any(u is None for u in usages) else ()
        )
        plan = plan_parts(
            usages,
            self.device.card.machine_json,
            max_launches=self.max_part_launches,
            participants=fallback,
        )
        records, all_bindings = [], {}
        for index in order:
            packet = packets[index]
            mapping = names(packet.graph, f"call{index}_")
            graph = renamed(packet.graph, mapping)
            bindings = {mapping[name]: at for name, at in packet.bindings.items()}
            records.append((graph, bindings, index))
            all_bindings.update(bindings)
        references = self.device._retain(all_bindings.values(), pinned=self.pinned)
        try:

            def compose(first, end):
                initial = records[first][0]
                graph = {
                    key: initial[key]
                    for key in ("format", "version", "target", "machine")
                }
                graph.update(
                    name="captured_split", consts={}, ports=[], scratch=[], body=[]
                )
                bindings, launches, deps = {}, [], []
                for part, bound, original in records[first:end]:
                    graph["consts"].update(
                        {
                            f"call{original}_{name}": value
                            for name, value in part["consts"].items()
                        }
                    )
                    graph["ports"].extend(part["ports"])
                    graph["scratch"].extend(part["scratch"])
                    graph["body"].extend(part["body"])
                    bindings.update(bound)
                    launches.append(json.dumps(part))
                    deps.append((original, tuple(dependencies[original])))
                physical = {
                    name: self.device.mmu.resolve(at)[1]
                    for name, at in bindings.items()
                }
                whole = first == 0 and end == plan.launches
                context: _PartContext = {}
                if not whole:
                    context = {
                        "part_nodes": list(plan.participants),
                        "part_first": first,
                        "launch_count": plan.launches,
                    }
                with rs.compiler_owner(self.device):
                    output = rs._rs.compile_split_graph(
                        json.dumps(graph),
                        rs.machine_json(self.device.machine),
                        physical,
                        zero_sign=rs._ZERO_SIGN.get(),
                        launches=launches,
                        default_node=self.device._node_of(None),
                        **context,
                    )
                compiled = generated.from_output(self.device, graph, output)
                compiled.compositional = plan.launches > 1
                return PreparedSplitGraph(
                    self.device,
                    compiled,
                    MappingProxyType(bindings),
                    tuple(dependencies) if whole else tuple(deps),
                    tuple(output["events"]),
                    [],
                    environment=physical,
                )

            # A size estimate is not admission: lowering can introduce extra
            # writer steps before bind_artifact gets a chance to refine. Split
            # only that planned interval, never retry the whole launch list.
            # Use a stack so a caller-selected part limit also bounds recursion.
            children, intervals = [], []
            pending = list(reversed(plan.intervals))
            while pending:
                first, end = pending.pop()
                try:
                    child = compose(first, end)
                except Exception as error:
                    if not capacity_error(error):
                        raise
                    if end - first == 1:
                        error.add_note(
                            f"retained launch {first} cannot compile with its complete join"
                        )
                        raise
                    middle = first + (end - first) // 2
                    pending.extend(((middle, end), (first, middle)))
                else:
                    children.append(child)
                    intervals.append((first, end))
            children = tuple(children)
            plan = replace(plan, intervals=tuple(intervals))
            if len(children) == 1:
                prepared = children[0]
            else:
                prepared = PreparedSplitGraph(
                    self.device,
                    children[0].compiled,
                    MappingProxyType(all_bindings),
                    tuple(dependencies),
                    tuple(event for child in children for event in child.events),
                    [],
                    environment={
                        name: self.device.mmu.resolve(at)[1]
                        for name, at in all_bindings.items()
                    },
                    planned_parts=children,
                )
            prepared.references = references
            prepared.part_plan = plan
            prepared.part_intervals = plan.intervals
            prepared.launch_factory = compose
            return prepared
        except BaseException:
            for arena, address in references:
                arena.unretain(address)
            raise
