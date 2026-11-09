"""Node-local residents publish together and retain an uncertain epoch's pins."""

import json
import struct
from types import MappingProxyType, SimpleNamespace

import pytest

from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.graph import split as split_graph
from kohakutpu.application.runtime.graph.split import PreparedSplitGraph
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import (
    Graph,
    PreparedMultiGraph,
    ResidentGraph,
)
from kohakutpu.application.runtime.os.multi import MultiOSCard
from kohakutpu.application.runtime.split import BoundGraphArtifact, NodePart, SlotLeases
from kohakutpu.stack.rs import _rs


def package(node, steps, kind=0):
    header = [0] * 12
    header[A.KA_PH_MAGIC] = 96 << 48 | A.KA_PKG_VERSION << 32 | A.KA_PKG_MAGIC
    header[A.KA_PH_SIZE] = 112 + len(steps) * 16 + 32
    header[A.KA_PH_COUNTS] = 1
    header[A.KA_PH_COUNTS2] = len(steps) << 16 | 1 << 32
    header[A.KA_PH_OFF_UNIT] = 96
    header[A.KA_PH_OFF_STEP] = 112
    header[A.KA_PH_OFF_PAY] = 112 + len(steps) * 16
    return (
        struct.pack(A.PKG_HDR_FMT, *header)
        + struct.pack("<QQ", kind << 32 | node << 16 | 2 << 8 | 1, 512)
        + b"".join(struct.pack("<QQ", *step) for step in steps)
        + struct.pack("<4Q", 123, 456, 789, 101112)
    )


class ResidentPair:
    """Completions exist only after every participant has received the epoch."""

    def __init__(self, nodes=2):
        self.nodes = set(range(nodes))
        self.requests = {}
        self.reads = []
        self.uploads = []
        self.stops = set()
        self.fail_node: int | None = None


class ResidentTransport:
    def __init__(self, pair, node):
        self.pair, self.node = pair, node

    def read(self, address, size):
        words = (0, 0, 0, 0)
        if address == A.OSTPU_OFF_READY:
            words = (A.OSTPU_MAGIC, 0x1000, 0x2000, self.node << 56 | 0x2000)
        elif address == A.OSTPU_OFF_CAP:
            words = (0, 32, 4096, 0)
        elif address == A.OSTPU_OFF_DONE:
            if self.node in self.pair.requests:
                assert self.pair.requests.keys() == self.pair.nodes, (
                    "collected before all publications"
                )
                self.pair.reads.append(self.node)
            words = (
                self.pair.requests.get(self.node, 0),
                0,
                A.OSTPU_CAP_LOCAL | A.OSTPU_CAP_SLOTS | A.OSTPU_CAP_EPOCH,
                0,
            )
        elif address == A.OSTPU_OFF_STATUS:
            status = -8 if self.node == self.pair.fail_node else 0
            return struct.pack("<q7Q", status, 0, 0, 100, 1, 0, 0, 0)
        return struct.pack("<4Q", *words).ljust(size, b"\0")[:size]

    def write(self, address, data):
        if (
            address == A.OSTPU_OFF_CMD
            and struct.unpack_from("<Q", data)[0] == A.OSTPU_OP_STOP
        ):
            raise AssertionError("cancellation overwrote the active command")
        if address == A.OSTPU_OFF_REQ:
            self.pair.requests[self.node] = struct.unpack_from("<Q", data)[0]
            if self.pair.requests[self.node] & A.OSTPU_REQ_CANCEL:
                self.pair.stops.add(self.node)
        elif address == 0x1000:
            self.pair.uploads.append((self.node, bytes(data)))

    def idle(self):
        raise AssertionError("all participants must be published before collection")


class Pins:
    def __init__(self):
        self.held = 0

    def retain_pinned(self, address):
        self.held += 1

    def unretain(self, address):
        self.held -= 1


def test_retained_parts_publish_together_and_failure_keeps_the_whole_epoch():
    pair = ResidentPair()
    residents = {
        node: ResidentGraph(ResidentTransport(pair, node), 0) for node in range(2)
    }
    steps = [(A.KA_OP_DISPATCH | 1 << 32, 0), (A.KA_OP_BARRIER, 0), (A.KA_OP_END, 0)]
    artifact = BoundGraphArtifact(
        "machine", tuple(NodePart(node, package(node, steps)) for node in range(2))
    )
    units = {(node, 1, 2): node for node in range(2)}
    pins = Pins()
    prepared = PreparedMultiGraph(
        artifact, residents, units, SlotLeases(), references=[(pins, 0x4000)]
    )
    upload_count = len(pair.uploads)
    for _ in range(2):
        pair.requests.clear()
        token = prepared.submit()
        with pytest.raises(RuntimeError, match="already in flight"):
            prepared.submit()
        with pytest.raises(RuntimeError, match="retained multi-part"):
            residents[0].prepare(Graph())
        assert set(token.wait()) == {0, 1}
        assert len(pair.uploads) == upload_count
    assert pins.held == 1
    pair.requests.clear()
    pair.reads.clear()
    pair.fail_node = 0
    with pytest.raises(RuntimeError, match="KDS status"):
        prepared.run()
    assert pair.reads[:2] == [0, 1]
    assert pair.stops == {0, 1}
    assert pins.held == 1 and all(r.poisoned for r in residents.values())
    with pytest.raises(RuntimeError, match="reset"):
        prepared.close()


def test_a_lease_cannot_be_reused_with_an_unconsumed_event():
    raw = package(0, [(A.KA_OP_RING | 9 << 32, 1), (A.KA_OP_END, 0)])
    with pytest.raises(ValueError, match="consume every released"):
        BoundGraphArtifact("machine", (NodePart(0, raw),))


@pytest.mark.parametrize("nodes", [1, 2])
def test_eager_linked_parts_publish_and_collect_on_every_node(nodes):
    pair = ResidentPair(nodes)
    residents = {
        node: ResidentGraph(ResidentTransport(pair, node), 0) for node in range(nodes)
    }
    card = MultiOSCard("multimesh_v9t6", residents, {}, HostMemory(), (0x2000, 0x3000))
    machine = card.machine_json
    fill = json.loads(machine)["machine"]["mover"]["modes"]["fill"]
    programs = {}
    for node in residents:
        launches = []
        for offset in (0, 256):
            address = 0x2000 + node * 512 + offset
            calls = [
                [
                    "mover",
                    [
                        [0x10, 1 | address << 4 | 1 << 44],
                        [0x18, 1 | 8 << 4 | 32 << 20],
                        [0x20, 0],
                        [0, (1 << 16) | fill],
                    ],
                ],
                ["end"],
            ]
            raw = _rs.package_build(
                json.dumps({"credit": 32, "mesh": node}),
                json.dumps(calls),
                machine,
                node,
            )
            launches.append(_rs.bind(raw, [], machine, node))
        linked = _rs.package_link(launches, machine, node)
        programs[node] = B.Prebuilt(linked, (), machine, node, (), False, card.fetch)
    firmware = BoundGraphArtifact(
        card.machine_digest,
        tuple(
            NodePart(node, program.bound(True)) for node, program in programs.items()
        ),
    )
    result = card.run(programs)
    assert card.active is not None
    assert card.active.artifact.parts == firmware.parts
    assert set(pair.requests) == set(residents)
    assert [node for node, _ in pair.uploads] == list(residents)
    assert pair.reads == list(residents)
    assert result["nodes"].keys() == residents.keys()
    card.release_graph()
    assert all(resident._retainer is None for resident in residents.values())


def test_model_entry_reuses_bound_parts_and_receiver_leases(monkeypatch):
    pair = ResidentPair()
    residents = {
        node: ResidentGraph(ResidentTransport(pair, node), 0) for node in range(2)
    }
    units = {(node, 1, 2): node for node in range(2)}
    dev = Device(
        "os",
        board="multimesh_v9t6",
        nodes=2,
        os_session=residents,
        os_units=units,
        os_memory=HostMemory(),
        os_arena=(0x2000, 0x3000),
    )
    dispatch = (A.KA_OP_DISPATCH | 1 << 32, 0)
    wait = (A.KA_OP_WAIT_SLOT | A.KA_STEP_F_CONSUME << 8 | 9 << 16 | 1 << 32, 0)
    parts = tuple(
        NodePart(
            node,
            package(
                node,
                [
                    dispatch,
                    (A.KA_OP_BARRIER, 0),
                    (A.KA_OP_RING | (1 - node) << 16 | 9 << 32, 1),
                    wait,
                    (A.KA_OP_END, 0),
                ],
                int.from_bytes(b"MG", "big"),
            ),
        )
        for node in range(2)
    )
    artifact = BoundGraphArtifact(dev.card.machine_digest, parts)
    bindings = []

    def bind(prepared):
        bindings.append(prepared)
        return artifact

    monkeypatch.setattr(split_graph, "bind", bind)
    address = dev.alloc(256)
    prepared = PreparedSplitGraph(
        dev,
        SimpleNamespace(name="split_model"),
        MappingProxyType({}),
        ((), (0,)),
        (),
        dev._retain([address]),
    )
    firmware_parts = {}

    def upload(node, raw):
        firmware_parts[node] = raw
        return SimpleNamespace(close=lambda: None)

    queues = {
        node: SimpleNamespace(
            prepare=lambda raw, node=node: upload(node, raw),
            prepare_list=lambda items: SimpleNamespace(close=lambda: None),
        )
        for node in range(2)
    }
    firmware = prepared.prepare_firmware(queues, dev.card.leases)
    owner = object()
    with pytest.raises(RuntimeError, match="already leased"):
        dev.card.leases.acquire(artifact.slot_leases, owner)
    firmware.close()
    uploads = None
    for _ in range(2):
        pair.requests.clear()
        pair.reads.clear()
        submitted = prepared.submit()
        active = dev.card.active
        assert active.artifact is firmware.artifact
        assert {p.node: p.package for p in active.artifact.parts} == firmware_parts
        assert active.artifact.slot_leases == frozenset({(0, 9), (1, 9)})
        assert pair.reads == [0, 1]
        assert submitted.result is not None
        assert submitted.result["nodes"].keys() == residents.keys()
        with pytest.raises(RuntimeError, match="already leased"):
            dev.card.leases.acquire(artifact.slot_leases, owner)
        dev.collect(submitted)
        if uploads is None:
            uploads = list(pair.uploads)
        else:
            assert pair.uploads == uploads
    assert bindings == [prepared]
    assert len(dev.runs) == 2
    prepared.close()
    dev.card.leases.acquire(artifact.slot_leases, owner)
    dev.card.leases.release(owner)
    dev.free(address)
