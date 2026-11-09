"""Behavior witnesses at the package-to-resident boundary, without a card."""

import pytest
from test_os_parts import Pins, ResidentPair, ResidentTransport, package

from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import (
    Graph,
    PreparedMultiGraph,
    ResidentGraph,
)
from kohakutpu.application.runtime.os.multi import MultiOSCard
from kohakutpu.application.runtime.split import BoundGraphArtifact, NodePart, SlotLeases


@pytest.mark.parametrize(
    "address,size", [(0x2000, 1), (0x2000, 33), (0x2001, 32), (-32, 32)]
)
def test_partial_mover_words_fail_before_graph_mutation(address, size):
    graph = Graph(de_stream=True)
    with pytest.raises(ValueError, match="word"):
        graph.move_de(bytes(size), address)
    assert graph.tasks == [] and graph.streams == {}
    assert graph.move_de(bytes(32), 0x2000)


def test_second_install_failure_rolls_back_claims_and_pins(monkeypatch):
    pair = ResidentPair()
    residents = {n: ResidentGraph(ResidentTransport(pair, n), 0) for n in range(2)}
    steps = [(A.KA_OP_DISPATCH | 1 << 32, 0), (A.KA_OP_BARRIER, 0), (A.KA_OP_END, 0)]
    artifact = BoundGraphArtifact(
        "machine", tuple(NodePart(n, package(n, steps)) for n in range(2))
    )
    pins, leases = Pins(), SlotLeases()

    def fail(*a, **kw):
        raise OSError("second upload failed")

    monkeypatch.setattr(residents[1], "_install", fail)
    with pytest.raises(OSError, match="second upload"):
        PreparedMultiGraph(
            artifact,
            residents,
            {(n, 1, 2): n for n in range(2)},
            leases,
            references=[(pins, 0x4000)],
        )
    assert pins.held == 0
    assert all(r._retainer is None and r.count == 0 for r in residents.values())


def test_local_cycles_are_a_maximum_not_a_sum_or_wall_duration():
    card = MultiOSCard.__new__(MultiOSCard)
    card.profile_sys = False
    first = card.result({0: {"cycles": 11}, 1: {"cycles": 29}}, 0.05)
    second = card.result({0: {"cycles": 17}, 1: {"cycles": 13}}, 0.003)
    assert first["cycles"] == first["phases"]["max_node_cycles"] == 29
    assert second["cycles"] == 17
    assert first["phases"]["makespan_seconds"] == 0.05
    assert second["phases"]["makespan_seconds"] == 0.003
