"""Capacity-directed partition at the retained composer's quiescent joins.

Comments identify source intervals, not a proof certificate. The complete emitted
local drain/gather/release closure is checked before a cut is used. Every part is
rebuilt and admitted, then checked with independent per-node package advancement.
"""

import hashlib
import json
import re
from dataclasses import dataclass, replace
from functools import cached_property
from types import SimpleNamespace

from kohakutpu.application.runtime.graph.limits import CapacityExceeded
from kohakutpu.application.runtime.graph.storage import borrow_scratch
from kohakutpu.application.runtime.split import BoundGraphArtifact, NodePart
from kohakutpu.language.text import module as M
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import arena_name

_CUT = re.compile(r"        # retained-cut ([0-9]+)\n?$")
_NODE = re.compile(r"    node ([0-9]+)\n?$")
_CAPACITY = re.compile(r"\b(?:L0\.PKG\.011|RUL\.005|RUL\.100)\b")


def capacity_error(error):
    return isinstance(error, CapacityExceeded) or bool(_CAPACITY.search(str(error)))


@dataclass(frozen=True)
class JoinedSource:
    prefix: str
    nodes: tuple
    bodies: tuple
    trailing_join: bool = False

    @classmethod
    def read(cls, source, *, trailing_join=False):
        lines = source.splitlines(keepends=True)
        function = next(
            (
                i
                for i, line in enumerate(lines)
                if line.startswith("fn captured_split.l1(")
            ),
            None,
        )
        if function is None:
            raise ValueError("capacity splitting requires retained composition")
        prefix = "".join(lines[: function + 1])
        nodes, bodies, pending = [], [], []
        for line in lines[function + 1 :]:
            node = _NODE.fullmatch(line)
            if node:
                if pending:
                    raise ValueError("retained node has an unclosed launch")
                nodes.append(int(node[1]))
                bodies.append([])
                continue
            cut = _CUT.fullmatch(line)
            if cut:
                if not bodies or int(cut[1]) != len(bodies[-1]):
                    raise ValueError("retained cut provenance is not contiguous")
                bodies[-1].append("".join(pending))
                pending = []
            elif bodies:
                pending.append(line)
            elif line.strip():
                raise ValueError("expected retained node block")
        if pending or not nodes or len(set(nodes)) != len(nodes):
            raise ValueError("malformed retained node intervals")
        count = len(bodies[0])
        if not count or any(len(body) != count for body in bodies):
            raise ValueError("retained nodes disagree on launch boundaries")
        result = cls(
            prefix, tuple(nodes), tuple(tuple(body) for body in bodies), trailing_join
        )
        result._check_joins()
        return result

    @property
    def count(self):
        return len(self.bodies[0])

    def _check_joins(self):
        coordinator = min(self.nodes)
        for index in range(self.count):
            slot = None
            for node, body in zip(self.nodes, self.bodies, strict=True):
                lines = body[index].splitlines()
                current = None
                if (index + 1 == self.count and not self.trailing_join) or len(
                    self.nodes
                ) == 1:
                    work = lines
                elif node == coordinator:
                    tail = lines[-len(self.nodes) :]
                    wait = (
                        re.fullmatch(
                            r"        wait slot ([0-9]+) n=([0-9]+) consume", tail[0]
                        )
                        if len(tail) == len(self.nodes)
                        else None
                    )
                    if wait is None or int(wait[2]) != len(self.nodes) - 1:
                        raise ValueError("retained cut is missing its gather")
                    current = int(wait[1])
                    expected = [
                        f"        ring mesh={other} slot={current}"
                        for other in sorted(self.nodes)
                        if other != node
                    ]
                    if tail[1:] != expected:
                        raise ValueError("retained cut is missing a release")
                    work = lines[: -len(self.nodes)]
                else:
                    wait = (
                        re.fullmatch(
                            r"        wait slot ([0-9]+) n=1 consume", lines[-1]
                        )
                        if len(lines) >= 2
                        else None
                    )
                    if wait is None:
                        raise ValueError("retained cut is missing its release wait")
                    current = int(wait[1])
                    if lines[-2] != f"        ring mesh={coordinator} slot={current}":
                        raise ValueError("retained cut is missing an arrival")
                    work = lines[:-2]
                if work and work[-1] != "        barrier":
                    raise ValueError("retained cut is missing its local drain")
                if current is not None:
                    if slot is not None and current != slot:
                        raise ValueError("retained join uses inconsistent slots")
                    slot = current

    def interval(self, first, end):
        if not 0 <= first < end <= self.count:
            raise ValueError("invalid retained launch interval")
        # Keep a nonfinal part's trailing handshake and only its definitions.
        prefix, keep = [], True
        for line in self.prefix.splitlines(keepends=True):
            definition = re.match(r"^(?:image|macro) retained([0-9]+)_", line)
            if definition:
                keep = first <= int(definition[1]) < end
            elif line.startswith("fn captured_split.l1("):
                keep = True
            if keep:
                prefix.append(line)
        return "".join(prefix) + "".join(
            f"    node {node}\n" + ("".join(body[first:end]) or "        barrier\n")
            for node, body in zip(self.nodes, self.bodies, strict=True)
        )


@dataclass(frozen=True)
class BoundGraphSequence:
    stages: tuple[BoundGraphArtifact, ...]
    intervals: tuple[tuple[int, int], ...]
    # The outer graph's public dependency/event schema, stored once, not once
    # per part. Stage-local provenance remains available in manifest['parts'].
    provenance: bytes = b""

    def __post_init__(self):
        if not self.stages or len(self.stages) != len(self.intervals):
            raise ValueError("a retained sequence requires complete intervals")
        nodes = tuple(part.node for part in self.stages[0].parts)
        end = 0
        for stage, (first, stop) in zip(self.stages, self.intervals, strict=True):
            if first != end or stop <= first:
                raise ValueError("retained sequence omits or repeats a launch")
            if tuple(part.node for part in stage.parts) != nodes:
                raise ValueError("retained parts must preserve every participant")
            if stage.machine_digest != self.stages[0].machine_digest:
                raise ValueError("retained parts belong to different machines")
            end = stop

    @property
    def parts(self) -> tuple[NodePart, ...]:
        return self.stages[-1].parts

    @property
    def slot_leases(self) -> frozenset[tuple[int, int]]:
        return self._slot_leases

    @cached_property
    def _slot_leases(self) -> frozenset[tuple[int, int]]:
        return frozenset(lease for stage in self.stages for lease in stage.slot_leases)

    @property
    def digest(self) -> bytes:
        return self._digest

    @cached_property
    def _digest(self) -> bytes:
        value = hashlib.blake2b(digest_size=32)
        value.update(self.provenance)
        for stage, interval in zip(self.stages, self.intervals, strict=True):
            value.update(stage.digest)
            value.update(json.dumps(interval).encode())
        return value.digest()

    @property
    def package_lists(self) -> dict[int, tuple[bytes, ...]]:
        return {
            part.node: tuple(
                next(p.package for p in stage.parts if p.node == part.node)
                for stage in self.stages
            )
            for part in self.parts
        }

    @property
    def manifest(self) -> bytes:
        return self._manifest

    @cached_property
    def _manifest(self) -> bytes:
        # Graph.package_parts expects an object with .get('dependencies'), not
        # a list. The public property above separately supplies GraphArtifact's
        # read-only bytes interface; only its private backing value is cached.
        source = self.provenance or self.stages[0].manifest
        manifest = json.loads(source) if source else {}
        manifest["parts"] = [
            {
                "interval": interval,
                "manifest": json.loads(stage.manifest) if stage.manifest else {},
            }
            for stage, interval in zip(self.stages, self.intervals, strict=True)
        ]
        return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()


def sequence_artifact(prepared, accepted):
    return BoundGraphSequence(
        tuple(artifact for artifact, _, _ in accepted),
        tuple(interval for _, interval, _ in accepted),
        json.dumps(
            {"dependencies": prepared.dependencies, "events": prepared.events},
            sort_keys=True,
        ).encode(),
    )


def _prove_sequence(prepared, stages):
    dev = prepared.device
    initial = {}
    for table in dev.tables.values():
        if table.host is not None:
            _, address = dev.mmu.resolve(table.addr)
            initial[address] = table.host
    runs = [{part.node: part.package for part in stage.parts} for stage in stages]
    # Only epoch boundaries import a real host join.
    with rs.compiler_owner(dev):
        verdict = json.loads(
            rs._rs.package_check_memory(
                runs + runs,
                dev.card.machine_json,
                initial,
                compositional=True,
                independent_packages=True,
                host_join_every=len(runs),
                retained_parts=True,
            )
        )
    # Preserve the checker's non-event work census for production-path
    # witnesses. This is evidence only; it neither relaxes admission nor changes
    # the proof budget. Rebinding clears the old epoch's census.
    prepared.proof_work = verdict.get("part_proof")
    if verdict["refused"]:
        raise ValueError(
            "retained part sequence proof refused: " + json.dumps(verdict["findings"])
        )


def bind_parts(prepared, bind_one, original_error):
    source = JoinedSource.read(prepared.compiled.l1.source)
    accepted = []

    def visit(first, end):
        compiled = replace(
            prepared.compiled, l1=M.Text(source.interval(first, end)), _progs={}
        )
        candidate = SimpleNamespace(
            device=prepared.device,
            compiled=compiled,
            bindings=prepared.bindings,
            environment=prepared.environment,
            scratch_limits=prepared.scratch_limits,
            admission=getattr(prepared, "admission", None),
            dependencies=prepared.dependencies,
            events=prepared.events,
            references=[],
            bound_references=[],
            bound_bindings=None,
        )
        try:
            artifact = bind_one(candidate)
        except Exception as error:
            if not capacity_error(error):
                raise
            if end - first == 1:
                error.add_note(
                    f"retained launch {first} cannot fit as a complete package"
                )
                raise
            middle = first + (end - first) // 2
            visit(first, middle)
            visit(middle, end)
        else:
            accepted.append((artifact, (first, end), candidate))

    try:
        if source.count == 1:
            raise original_error
        middle = source.count // 2
        visit(0, middle)
        visit(middle, source.count)
        stages = tuple(item[0] for item in accepted)
        _prove_sequence(prepared, stages)
        result = sequence_artifact(prepared, accepted)
        references = [
            ref for _, _, candidate in accepted for ref in candidate.bound_references
        ]
        prepared.references.extend(references)
        prepared.bound_references = references
        prepared.bound_bindings = accepted[0][2].bound_bindings
        prepared.part_bindings = tuple(c.bound_bindings for _, _, c in accepted)
        prepared.part_compiled = tuple(c.compiled for _, _, c in accepted)
        prepared.resource_usage = tuple(c.resource_usage for _, _, c in accepted)
        return result
    except BaseException:
        for _, _, candidate in accepted:
            for arena, address in candidate.bound_references:
                arena.unretain(address)
        raise


def reserve_part_scratch(prepared):
    """Reserve the shared peak envelope before binding a smaller first part."""
    peak = {}
    for candidate in prepared.planned_parts or (prepared,):
        for arena in candidate.compiled.scratch or ():
            name = arena_name(arena)
            if name not in peak or peak[name]["bytes"] < arena["bytes"]:
                peak[name] = arena
    combined = replace(prepared.compiled, scratch=list(peak.values()))
    return borrow_scratch(
        prepared.device,
        combined,
        dict(prepared.bindings),
        prepared.scratch_limits,
    )


def bind_planned_parts(prepared, bind_one, original_error: Exception | None = None):
    """Admit a pre-composition plan; refine only an underestimated interval."""
    accepted = []
    total = prepared.part_plan.launches
    reserved = reserve_part_scratch(prepared)
    reservation_refs = []
    completed = False

    def visit(candidate, first, end, error: Exception | None = None):
        candidate.scratch_limits = prepared.scratch_limits
        candidate.admission = prepared.admission
        source = JoinedSource.read(
            candidate.compiled.l1.source, trailing_join=end < total
        )
        if (
            source.nodes != prepared.part_plan.participants
            or source.count != end - first
        ):
            raise ValueError(
                "retained part changed its planned participants or interval"
            )
        try:
            if error is not None:
                raise error
            artifact = bind_one(candidate)
        except Exception as failure:
            if not capacity_error(failure) or end - first == 1:
                if capacity_error(failure):
                    failure.add_note(
                        f"retained launch {first} cannot fit with its complete join"
                    )
                raise
            middle = first + (end - first) // 2
            visit(prepared.launch_factory(first, middle), first, middle)
            visit(prepared.launch_factory(middle, end), middle, end)
        else:
            accepted.append((artifact, (first, end), candidate))

    try:
        # bind_one releases unpinned windows on exceptions. Hold the peak while
        # refining so a smaller first child cannot strand subsequent children.
        for address in set(reserved.values()):
            entry, _ = prepared.device.mmu.resolve(address)
            reservation_refs.extend(
                prepared.device._retain(
                    [address],
                    pinned=entry.physical in entry.arena.released,
                )
            )
        candidates = prepared.planned_parts or (prepared,)
        for candidate, (first, end) in zip(
            candidates, prepared.part_intervals, strict=True
        ):
            visit(candidate, first, end, original_error)
        stages = tuple(artifact for artifact, _, _ in accepted)
        _prove_sequence(prepared, stages)
        result = sequence_artifact(prepared, accepted)
        references = [ref for _, _, c in accepted for ref in c.bound_references]
        prepared.references.extend(references)
        prepared.bound_references = references
        prepared.bound_bindings = accepted[0][2].bound_bindings
        prepared.part_bindings = tuple(c.bound_bindings for _, _, c in accepted)
        prepared.part_compiled = tuple(c.compiled for _, _, c in accepted)
        prepared.resource_usage = tuple(c.resource_usage for _, _, c in accepted)
        completed = True
        return result
    except BaseException:
        for _, _, candidate in accepted:
            for arena, address in candidate.bound_references:
                arena.unretain(address)
            candidate.references.clear()
            candidate.bound_references = []
            candidate.bound_bindings = None
        raise
    finally:
        for arena, address in reservation_refs:
            arena.unretain(address)
        if not completed:
            prepared.device._release_windows()
