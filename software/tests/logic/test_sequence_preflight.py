"""An invalid later participant set must not publish the valid first part."""

import pytest
from test_kaos_sequence import Pair, Transport, artifact_for
from test_os_parts import Pins

from kohakutpu.application.runtime.graph import split as split_graph
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import (
    Graph,
    PreparedMultiGraph,
    ResidentGraph,
    Task,
)
from kohakutpu.application.runtime.split import SlotLeases


@pytest.mark.parametrize("later_nodes", [(0,), (0, 1, 1), (0, 2)])
def test_invalid_later_part_is_rejected_before_any_upload(monkeypatch, later_nodes):
    artifact = artifact_for(((0, 1), later_nodes))

    def graphs(stage, units, **options):
        result = {}
        for part in stage.parts:
            graph = Graph()
            graph.add(Task(A.KDS_UNIT, words=(2, 1, 0, 0)))
            result[part.node] = graph
        return result

    monkeypatch.setattr(Graph, "package_parts", staticmethod(graphs))
    pair = Pair(None)
    residents = {node: ResidentGraph(Transport(pair, node), 0) for node in (0, 1)}
    pins, leases = Pins(), SlotLeases()
    # Base's existing single-graph adapter publishes the first part instead.
    adapter = getattr(split_graph, "PreparedMultiSequence", PreparedMultiGraph)
    with pytest.raises(ValueError, match="identical participants"):
        adapter(artifact, residents, {}, leases, references=[(pins, 256)])
    assert not pair.uploads and not pair.requests
    assert pins.held == 0
    assert all(resident._retainer is None for resident in residents.values())
