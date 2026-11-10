"""Host-installed sequence lifecycle, with real resident encoding/mailboxes.

The small arithmetic task interpreter is a transport double. The separate
runtime witness checks actual kernel package arithmetic and capacity admission.
"""

import struct
from dataclasses import dataclass

import pytest
from test_os_parts import Pins, ResidentPair, ResidentTransport

from kohakutpu.application.runtime.graph import split as split_graph
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import (
    Graph,
    PreparedMultiGraph,
    ResidentGraph,
    Task,
)
from kohakutpu.application.runtime.split import NodePart, SlotLeases


@dataclass(frozen=True)
class Stage:
    """Typed translation input; package translation is replaced in these tests."""

    parts: tuple[NodePart, ...]
    digest: bytes
    manifest: bytes = b""
    slot_leases: frozenset[tuple[int, int]] = frozenset()


@dataclass(frozen=True)
class Sequence(Stage):
    stages: tuple[Stage, ...] = ()

    @property
    def package_lists(self) -> dict[int, tuple[bytes, ...]]:
        return {
            part.node: tuple(
                next(p.package for p in stage.parts if p.node == part.node)
                for stage in self.stages
            )
            for part in self.parts
        }


def artifact_for(nodes_by_stage, slot_leases=frozenset()) -> Sequence:
    stages = tuple(
        Stage(tuple(NodePart(node, b"") for node in nodes), bytes([index]))
        for index, nodes in enumerate(nodes_by_stage)
    )
    return Sequence(
        stages[0].parts, stages[0].digest, slot_leases=slot_leases, stages=stages
    )


class Pair(ResidentPair):
    def __init__(self, failure):
        super().__init__()
        self.failure = failure
        self.programs = {}
        self.finished = {}
        self.values = {0: 0, 1: 0}
        self.executed = []


class Transport(ResidentTransport):
    def write(self, address, data):
        pair = self.pair
        if address == 0x1000:
            # No next-part upload may overwrite a task array still in flight,
            # including the peer that the host has not collected yet.
            assert all(
                pair.finished.get(n) == token for n, token in pair.requests.items()
            )
            pair.programs[self.node] = struct.unpack_from(
                "<4Q", data, A.KDS_TASK_OFF_WORDS
            )[:2]
        super().write(address, data)

    def read(self, address, size):
        pair, node = self.pair, self.node
        if address == A.OSTPU_OFF_DONE and pair.requests:
            assert len(set(pair.requests.values())) == 1, (
                "collected before all publications"
            )
        if address == A.OSTPU_OFF_STATUS:
            multiply, add = pair.programs[node]
            failed = node == 1 and add == 4 and pair.failure is not None
            if failed and pair.failure == "timeout":
                raise TimeoutError("middle part mailbox timeout")
            if not failed:
                pair.values[node] = pair.values[node] * multiply + add
                pair.executed.append((node, add))
                pair.finished[node] = pair.requests[node]
            return struct.pack(
                "<q7Q", -8 if failed else 0, 0, 0, 10 + node + add, 1, 0, 0, 0
            )
        return super().read(address, size)


@pytest.mark.parametrize("failure", [None, "status", "timeout"])
def test_kaos_reinstalls_each_part_and_stops_before_a_successor_after_failure(
    monkeypatch, failure
):
    operations = ((2, 1), (3, 4), (2, 7))
    artifact = artifact_for(((0, 1),) * 3, frozenset({(0, 9), (1, 9)}))

    def graphs(stage, units, **options):
        multiply, add = operations[stage.digest[0]]
        result = {}
        for node in range(2):
            graph = Graph()
            graph.add(Task(A.KDS_UNIT, words=(multiply, add, 0, 0)))
            result[node] = graph
        return result

    monkeypatch.setattr(Graph, "package_parts", staticmethod(graphs))
    pair = Pair(failure)
    residents = {node: ResidentGraph(Transport(pair, node), 0) for node in range(2)}
    pins, leases = Pins(), SlotLeases()
    # On base use its existing single-resident adapter, rather than failing an
    # import of a new API. It executes only part zero: wrong arithmetic on the
    # healthy case and no middle failure in the two fault cases.
    adapter = getattr(split_graph, "PreparedMultiSequence", PreparedMultiGraph)
    prepared = adapter(artifact, residents, {}, leases, references=[(pins, 256)])
    if failure is None:
        for seed in (1, 5):
            pair.values = {node: seed + node for node in residents}
            token = prepared.submit(timeout_cycles=12345)
            with pytest.raises(RuntimeError, match="already in flight"):
                prepared.submit()
            result = token.wait()
            for node in residents:
                assert pair.values[node] == (((seed + node) * 2 + 1) * 3 + 4) * 2 + 7
                assert result[node]["submissions"] == 3
                assert result[node]["cycles"] == sum(
                    part["cycles"] for part in result[node]["part_results"]
                )
                assert token.wait() is result  # collection does not execute it again
        prepared.close()
        assert pins.held == 0
        leases.acquire(artifact.slot_leases, object())
    else:
        with pytest.raises(
            (RuntimeError, TimeoutError), match="KDS status|middle part"
        ):
            prepared.submit().wait()
        assert pair.values[0] == (0 * 2 + 1) * 3 + 4
        assert pair.values[1] == 0 * 2 + 1
        assert (0, 7) not in pair.executed and (1, 7) not in pair.executed
        assert all(resident.poisoned for resident in residents.values())
        assert pins.held == 1 and pair.stops == {0, 1}
        with pytest.raises(RuntimeError, match="already leased"):
            leases.acquire(artifact.slot_leases, object())
        with pytest.raises(RuntimeError, match="reset"):
            prepared.close()
