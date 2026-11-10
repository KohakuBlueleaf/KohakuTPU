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
from kohakutpu.application.runtime.split import BoundGraphArtifact, NodePart
from kohakutpu.language.text import module as M
from kohakutpu.stack import rs

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

    @classmethod
    def read(cls, source):
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
        result = cls(prefix, tuple(nodes), tuple(tuple(body) for body in bodies))
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
                # The last launch needs only its local close. An inactive node
                # has no body, but must still participate in every intermediate join.
                if index + 1 == self.count or len(self.nodes) == 1:
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
        # Keep the trailing handshake on a nonfinal part. Merely composing the
        # subset afresh would delete that edge and permit early scratch reuse.
        # Definitions are namespaced per launch by retained.rs. Omit only
        # definitions belonging to excluded launches; otherwise every part
        # reparses all of a large graph's image bodies.
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
        # The successful terminal CQ's step index is relative to the last package.
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
    def _digest(self):
        value = hashlib.blake2b(digest_size=32)
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
        # All intervals retain the original composition provenance.
        return self.stages[0].manifest


def _prove_sequence(prepared, stages):
    dev = prepared.device
    initial = {}
    for table in dev.tables.values():
        if table.host is not None:
            _, address = dev.mmu.resolve(table.addr)
            initial[address] = table.host
    runs = [{part.node: part.package for part in stage.parts} for stage in stages]
    # No invented all-node host fence between capacity parts. Two epochs also
    # check reuse of slot counters and package-local fetch/patch state.
    with rs.compiler_owner(dev):
        verdict = json.loads(
            rs._rs.package_check_memory(
                runs + runs,
                dev.card.machine_json,
                initial,
                compositional=True,
                independent_packages=True,
                host_join_every=len(runs),
            )
        )
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
        result = BoundGraphSequence(stages, tuple(item[1] for item in accepted))
        references = [
            ref for _, _, candidate in accepted for ref in candidate.bound_references
        ]
        prepared.references.extend(references)
        prepared.bound_references = references
        prepared.bound_bindings = accepted[0][2].bound_bindings
        prepared.part_compiled = tuple(
            candidate.compiled for _, _, candidate in accepted
        )
        prepared.resource_usage = tuple(
            candidate.resource_usage for _, _, candidate in accepted
        )
        return result
    except BaseException:
        for _, _, candidate in accepted:
            for arena, address in candidate.bound_references:
                arena.unretain(address)
        raise
