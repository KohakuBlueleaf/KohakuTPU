"""KAOS collection-time installs belong to the original launch, not its waiter."""

import json
import struct
from dataclasses import dataclass
from types import SimpleNamespace

import record
from test_package_check import install

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


class Transport:
    """Arithmetic mailbox double; all encoding and sequencing are production."""

    def __init__(self, node):
        self.node = node
        self.tasks = 0x1000 + node * 0x4000
        self.sequence = 0
        self.raw = b""
        self.value = 0
        self.publications = []

    def read(self, address, size):
        words = (0, 0, 0, 0)
        if address == A.OSTPU_OFF_READY:
            words = (A.OSTPU_MAGIC, self.tasks, 0x2000, self.node << 56 | 0x2000)
        elif address == A.OSTPU_OFF_CAP:
            words = (0, 32, 4096, 0)
        elif address == A.OSTPU_OFF_DONE:
            words = (
                self.sequence,
                0,
                A.OSTPU_CAP_LOCAL | A.OSTPU_CAP_SLOTS | A.OSTPU_CAP_EPOCH,
                0,
            )
        elif address == A.OSTPU_OFF_STATUS:
            multiply, add = struct.unpack_from("<4Q", self.raw, A.KDS_TASK_OFF_WORDS)[
                :2
            ]
            self.value = self.value * multiply + add
            return struct.pack("<q7Q", 0, 0, 0, 10, 1, 0, 0, 0)
        return struct.pack("<4Q", *words).ljust(size, b"\0")[:size]

    def write(self, address, raw):
        if address == self.tasks:
            self.raw = bytes(raw)
        elif address == A.OSTPU_OFF_REQ:
            words = struct.unpack("<4Q", raw)
            self.sequence = words[0]
            self.publications.append(
                dict(
                    node=self.node,
                    address=self.tasks,
                    nbytes=len(self.raw),
                    graph_epoch=words[A.OSTPU_REQ_EPOCH_WORD],
                    sequence=self.sequence,
                )
            )

    def idle(self):
        raise AssertionError("synchronous mailbox double must already be done")


def test_collection_time_parts_keep_their_original_launch_on_every_replay(
    tmp_path, monkeypatch
):
    trace = record.Trace(tmp_path)
    install(trace, monkeypatch)
    stages = tuple(
        Stage(tuple(NodePart(node, b"") for node in (0, 1)), bytes([index]))
        for index in range(3)
    )
    artifact = Sequence(stages[0].parts, stages[0].digest, stages=stages)
    operations = ((2, 1), (3, 4), (2, 7))

    def graphs(stage, units, **options):
        multiply, add = operations[stage.digest[0]]
        result = {}
        for node in (0, 1):
            graph = Graph()
            graph.add(Task(A.KDS_UNIT, words=(multiply, add, 0, 0)))
            result[node] = graph
        return result

    monkeypatch.setattr(Graph, "package_parts", staticmethod(graphs))
    transports = {node: Transport(node) for node in (0, 1)}
    residents = {node: ResidentGraph(io, 0) for node, io in transports.items()}
    # Base reaches real mailbox execution, but runs only the first operation.
    adapter = getattr(split_graph, "PreparedMultiSequence", PreparedMultiGraph)
    prepared = adapter(artifact, residents, {}, SlotLeases())
    dev = SimpleNamespace(
        card=SimpleNamespace(sessions=residents),
        mmu=SimpleNamespace(resolve=lambda at: (None, at)),
        recording=False,
    )
    compiled = SimpleNamespace(name="sequence", l1_text="", l2_text="", scratch=[])
    expected = []
    for seed in (1, 5):
        for node, io in transports.items():
            io.value = seed + node
            io.publications.clear()
        trace.launch(dev, compiled, {}, "graph", None, 0)
        token = prepared.submit()
        trace.submitted(0)
        trace.opened(dict(name="unrelated_waiter", phase="ordinary", kind="dsl"))
        token.wait()
        trace.submitted(0)
        for node, io in transports.items():
            assert io.value == (((seed + node) * 2 + 1) * 3 + 4) * 2 + 7
        expected.append(
            [
                dict(publication, part=index)
                for io in transports.values()
                for index, publication in enumerate(io.publications)
            ]
        )
    prepared.close()
    trace.save({})
    rows = [
        json.loads(line)
        for line in (tmp_path / "launches.jsonl").read_text().splitlines()
    ]
    for replay, publications in enumerate(expected):
        spans = rows[replay * 2]["packages"]
        assert not rows[replay * 2 + 1].get("packages")
        for publication in publications:
            span = next(
                item
                for item in spans
                if (item["node"], item["part"])
                == (publication["node"], publication["part"])
            )
            assert {key: span[key] for key in publication} == publication
            assert span["storage_format"] == "kaos_tasks"
            assert span["package_address"] is None
