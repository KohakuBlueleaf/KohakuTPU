"""Public sequence completion covers all parts, including lists longer than 256."""

import pytest
from test_kaos_sequence import Pair, Sequence, Stage, Transport
from test_os_parts import Pins

from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import (
    Graph,
    MultiCompletion,
    ResidentGraph,
    Task,
)
from kohakutpu.application.runtime.os.sequence import PreparedMultiSequence
from kohakutpu.application.runtime.split import NodePart, SlotLeases


def prepare(monkeypatch, count, failure=None):
    parts = tuple(NodePart(node, b"") for node in (0, 1))
    stages = tuple(Stage(parts, index.to_bytes(8, "little")) for index in range(count))
    artifact = Sequence(
        parts, b"whole", stages=stages, slot_leases=frozenset({(0, 9), (1, 9)})
    )

    def graphs(stage, units, **options):
        add = int.from_bytes(stage.digest, "little") + 1
        result = {}
        for node in (0, 1):
            graph = Graph()
            graph.add(Task(A.KDS_UNIT, words=(1, add, 0, 0)))
            result[node] = graph
        return result

    monkeypatch.setattr(Graph, "package_parts", staticmethod(graphs))
    pair = Pair(failure)
    residents = {node: ResidentGraph(Transport(pair, node), 0) for node in (0, 1)}
    pins, leases = Pins(), SlotLeases()
    prepared = PreparedMultiSequence(
        artifact, residents, {}, leases, references=[(pins, 256)]
    )
    return prepared, pair, pins, leases


def test_long_sequence_has_one_completion_and_replays_each_part_once(monkeypatch):
    prepared, pair, pins, leases = prepare(monkeypatch, 260)
    public = []
    installed = []
    install = prepared._install_part

    def observe(index):
        # In the old adapter the public token already held part-zero results.
        assert public[-1].results is None
        installed.append(index)
        install(index)

    monkeypatch.setattr(prepared, "_install_part", observe)
    try:
        for epoch in range(2):
            # On warm submit the previous token is legitimately complete; the
            # part-zero reinstall precedes creation of the next public token.
            monkeypatch.setattr(prepared, "_install_part", install)
            token = prepared.submit()
            public.append(token)
            monkeypatch.setattr(prepared, "_install_part", observe)
            assert token.results is None
            impostor = MultiCompletion(prepared, token.tickets)
            with pytest.raises(RuntimeError, match="no collectable"):
                prepared.collect(impostor)
            assert not prepared.poisoned
            result = token.wait()
            assert token.wait() is result
            assert installed == list(range(1, 260)) * (epoch + 1)
            for node in (0, 1):
                assert pair.values[node] == sum(range(1, 261)) * (epoch + 1)
                assert result[node]["completed_parts"] == 260
                assert result[node]["submissions"] == 260
                assert len(result[node]["part_results"]) == 260
            assert pair.executed == [
                (node, add)
                for _ in range(epoch + 1)
                for add in range(1, 261)
                for node in (0, 1)
            ]
    finally:
        prepared.close()
    assert pins.held == 0
    leases.acquire(prepared.artifact.slot_leases, object())


@pytest.mark.parametrize("failure", ["status", "timeout"])
def test_failed_later_part_never_publishes_partial_completion(monkeypatch, failure):
    prepared, pair, pins, leases = prepare(monkeypatch, 6, failure)
    token = prepared.submit()
    install = prepared._install_part

    def observe(index):
        assert token.results is None
        install(index)

    monkeypatch.setattr(prepared, "_install_part", observe)
    with pytest.raises((RuntimeError, TimeoutError), match="KDS status|middle part"):
        token.wait()
    assert token.results is None
    assert all(add <= 4 for _, add in pair.executed)
    assert pins.held == 1
    assert prepared.poisoned
    with pytest.raises(RuntimeError, match="reset"):
        prepared.close()
    with pytest.raises(RuntimeError, match="already leased"):
        leases.acquire(prepared.artifact.slot_leases, object())
