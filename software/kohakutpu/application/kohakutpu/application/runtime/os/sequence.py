"""Ordered capacity parts on the existing single-graph resident ABI.

Unlike firmware HOSTL, switching KAOS parts requires all-node completion and a
host upload. A warm replay reinstalls every part; it is not host-free retention.
"""

from dataclasses import dataclass

from kohakutpu.application.runtime.os.graph import Graph, PreparedMultiGraph
from kohakutpu.application.runtime.split import NodePart, RetainedArtifact


@dataclass(frozen=True)
class _StageGrant:
    """First stage's bytes with the entire sequence's ownership grant."""

    parts: tuple[NodePart, ...]
    digest: bytes
    manifest: bytes
    slot_leases: frozenset[tuple[int, int]]


class PreparedMultiSequence(PreparedMultiGraph):
    def __init__(
        self,
        artifact: RetainedArtifact,
        residents,
        units,
        leases,
        *,
        references=(),
        unit_types=None,
        **options,
    ):
        if len(artifact.stages) < 2:
            raise ValueError("a resident sequence needs at least two parts")
        participants = {part.node for part in artifact.parts}
        if not participants or len(participants) != len(artifact.parts):
            raise ValueError("resident sequence needs unique participants")
        for stage in artifact.stages:
            nodes = [part.node for part in stage.parts]
            if set(nodes) != participants or len(nodes) != len(participants):
                raise ValueError(
                    "resident sequence parts must have identical participants"
                )
        if not participants.issubset(residents):
            raise ValueError("resident sequence is missing a participant session")
        node_ids = {node: residents[node].data_ref >> 56 for node in participants}
        # Reject every interval before publishing any upload. The base owner
        # claims the sessions/leases and repeats first-part encoding under those
        # claims before installing it; no unchecked bytes cross that boundary.
        encoded = []
        for stage in artifact.stages:
            graphs = Graph.package_parts(
                stage, units, node_ids=node_ids, unit_types=unit_types, **options
            )
            if set(graphs) != participants:
                raise ValueError("encoded resident part changed participants")
            encoded.append(
                {
                    node: (residents[node].encode(graph), len(graph.tasks))
                    for node, graph in graphs.items()
                }
            )
        first = artifact.stages[0]

        super().__init__(
            _StageGrant(
                first.parts, first.digest, first.manifest, artifact.slot_leases
            ),
            residents,
            units,
            leases,
            references=references,
            unit_types=unit_types,
            **options,
        )
        self.artifact = artifact
        self.sequence = artifact
        self._encoded_parts = tuple(encoded)
        self._installed_part = 0
        self._timeout_cycles = 1 << 26

    def _install_part(self, index):
        stage = self.sequence.stages[index]
        for node, resident in self.residents.items():
            raw, count = self._encoded_parts[index][node]
            resident._install(raw, count, _owner=self, identity=stage.digest)
        self._installed_part = index

    def submit(self, timeout_cycles=1 << 26):
        with self._lock:
            if self.closed or self.poisoned or self.pending is not None:
                raise RuntimeError(
                    "resident sequence is closed, failed or already in flight"
                )
            try:
                if self._installed_part != 0:
                    self._install_part(0)
                # This is a per-part device bound, as in a HOSTL RUN descriptor;
                # do not call it a whole-sequence wall-clock deadline.
                self._timeout_cycles = timeout_cycles
                return super().submit(timeout_cycles)
            except BaseException:
                if not self.poisoned:
                    self._cancel_all()
                raise

    def collect(self, token):
        with self._lock:
            if token.owner is not self:
                raise ValueError("completion belongs to another resident sequence")
            if token.results is not None:
                return token.results
            try:
                all_results = [super().collect(token)]
                for index in range(1, len(self._encoded_parts)):
                    self._install_part(index)
                    next_token = super().submit(self._timeout_cycles)
                    all_results.append(super().collect(next_token))
                # Do not combine sum(cycles) with the terminal part's unrelated
                # counters. Keep stats in their original per-part scopes.
                total = {}
                for node in self.residents:
                    pieces = [item[node] for item in all_results]
                    total[node] = dict(
                        cycles=sum(item["cycles"] for item in pieces),
                        completed_parts=sum(item["completed"] for item in pieces),
                        submissions=sum(item["submissions"] for item in pieces),
                        part_results=pieces,
                        resident_installs=len(pieces),
                        resident_install_bytes=sum(
                            len(part[node][0]) for part in self._encoded_parts
                        ),
                        retained_sequence_host_installs=True,
                        cycle_scope="sum_of_node_local_part_durations_excludes_host_transitions",
                    )
                token.results = total
                return total
            except BaseException:
                token.results = None
                if not self.poisoned:
                    self._cancel_all()
                raise
