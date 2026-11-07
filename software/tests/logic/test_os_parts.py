"""Node-local residents publish together and retain an uncertain epoch's pins."""

import struct

import pytest

from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import (
    Graph,
    PreparedMultiGraph,
    ResidentGraph,
)
from kohakutpu.application.runtime.split import BoundGraphArtifact, NodePart, SlotLeases


def package(node, steps):
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
        + struct.pack("<QQ", node << 16 | 2 << 8 | 1, 512)
        + b"".join(struct.pack("<QQ", *step) for step in steps)
        + struct.pack("<4Q", 123, 456, 789, 101112)
    )


class ResidentPair:
    """Completions exist only after both CPUs have received the epoch."""

    def __init__(self):
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
                assert len(self.pair.requests) == 2, "collected before all publications"
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
