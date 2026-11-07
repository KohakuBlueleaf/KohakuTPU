"""One retained bound graph on the KAOS lowerer and firmware queue protocol double."""

import struct
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.node.queue.layout import Layout
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import Graph
from kohakutpu.application.runtime.split import (
    BoundGraphArtifact,
    NodePart,
    PreparedFirmwareGraph,
    SlotLeases,
)

MACHINE = Path(__file__).resolve().parents[3] / "machines" / "multimesh_v9t6.json"


def package(node, steps, words, extra=()):
    payload = (112 + len(steps) * 16 + 31) // 32 * 32
    header = [0] * 12
    header[A.KA_PH_MAGIC] = 96 << 48 | A.KA_PKG_VERSION << 32 | A.KA_PKG_MAGIC
    header[A.KA_PH_SIZE] = payload + 32 * (1 + len(extra))
    header[A.KA_PH_COUNTS] = 1 + len(extra)
    header[A.KA_PH_COUNTS2] = len(steps) << 16 | 1 << 32
    header[A.KA_PH_OFF_UNIT] = 96
    header[A.KA_PH_OFF_STEP] = 112
    header[A.KA_PH_OFF_PAY] = payload
    raw = (
        struct.pack(A.PKG_HDR_FMT, *header)
        + struct.pack("<QQ", node << 16 | 2 << 8 | 1, 512)
        + b"".join(struct.pack("<QQ", *step) for step in steps)
    )
    return raw.ljust(payload, b"\0") + b"".join(
        struct.pack("<4Q", *group) for group in (words, *extra)
    )


def step(op, unit=0, count=0, arg=0, flags=0):
    return op | flags << 8 | unit << 16 | count << 32, arg


def artifact():
    left = package(
        0,
        [
            step(A.KA_OP_DISPATCH, count=1),
            step(A.KA_OP_AWAIT, count=1),
            step(A.KA_OP_RING, unit=1, count=9, arg=1 | 1 << 16),
            step(A.KA_OP_WAIT_SLOT, unit=9, count=1, flags=A.KA_STEP_F_CONSUME),
            step(A.KA_OP_AWAIT, count=1),
            step(A.KA_OP_END),
        ],
        (1, 0, 0x123456789ABCDEF0, 0),
    )
    right = package(
        1,
        [
            step(A.KA_OP_WAIT_SLOT, unit=9, count=1, flags=A.KA_STEP_F_CONSUME),
            step(A.KA_OP_DISPATCH, count=1),
            step(A.KA_OP_AWAIT, count=1),
            step(A.KA_OP_ENGINE, count=1, arg=1),
            step(A.KA_OP_END),
        ],
        (2, 8, 0, 0x55AA),
        ((A.KOS_DE_IL_RING, 9 << 8 | 1 << 32, 0, 0),),
    )
    return BoundGraphArtifact("machine", (NodePart(0, left), NodePart(1, right)))


def entries(graph, index):
    raw = graph.streams[index]
    for k in range(graph.tasks[index].words[1]):
        group = struct.unpack_from("<4Q", raw, 32 * (k // 3))
        yield (group[0] >> (8 * (k % 3))) & 255, group[k % 3 + 1]


class Engine:
    """Opaque unit instructions: store-immediate and read/xor with a peer marker.

    The adapters do not know these operations. The two independent decoders below
    reach the same execution double through the actual instruction words.
    """

    def __init__(self):
        self.queues = [deque(), deque()]
        self.counts = [0, 0]
        self.slots = [0, 0]
        self.mailbox = [[0] * 5, [0] * 5]
        self.output = bytearray(16)

    def execute(self, node, words):
        op, dst, source, argument = words
        if op == 1:
            value = source
        else:
            assert op == 2
            value = struct.unpack_from("<Q", self.output, source)[0] ^ argument
            self.counts[0] += 1
        struct.pack_into("<Q", self.output, dst, value)
        self.counts[node] += 1

    def issue(self, node):
        queue = self.queues[node]
        if not queue:
            return False
        code, value = queue[0]
        if code == A.KOS_DE_WAIT_CODE:
            assert value >> 56 == 0
            target = value & A.KOS_DE_CTR_MASK
            if self.counts[node] < target:
                return False
        elif code == A.KOS_DE_MB_GO:
            self.execute(node, self.mailbox[node][1:])
        elif code < A.KOS_DE_MB_GO:
            self.mailbox[node][code] = value
        else:
            raise AssertionError(f"unexpected descriptor {code}")
        queue.popleft()
        return True

    def finish(self):
        assert self.counts == [2, 1] and self.slots == [0, 0]
        self.counts[:] = [0, 0]
        return bytes(self.output)

    def run(self, graphs):
        done = [set(), set()]
        total = sum(len(g.tasks) for g in graphs.values())
        for _ in range(1000):
            progress = False
            for node, graph in graphs.items():
                for index, task in enumerate(graph.tasks):
                    if index in done[node] or not set(task.pred) <= done[node]:
                        continue
                    if task.kind == A.KDS_DE_FEED:
                        self.queues[node].extend(entries(graph, index))
                    elif task.kind == A.KDS_WAIT_SLOT:
                        if self.slots[node] < task.words[1]:
                            continue
                        self.slots[node] -= task.words[1]
                    elif task.kind == A.KDS_RELEASE_SLOT:
                        self.slots[task.words[2]] += task.words[1]
                    elif task.kind in (A.KDS_DE_DRAIN, A.KDS_DE_FINISH):
                        if self.queues[node]:
                            continue
                    else:
                        assert task.kind == A.KDS_NOP
                    done[node].add(index)
                    progress = True
                progress |= self.issue(node)
            if sum(map(len, done)) == total:
                return self.finish()
            assert progress, "slot acquire or native-peer frontier cannot progress"
        raise AssertionError("descriptor graph did not terminate")

    def firmware(self, packages):
        """Cooperative KAPK step interpreter, independent of Graph's task lowering."""
        headers = [struct.unpack_from(A.PKG_HDR_FMT, raw) for raw in packages]
        pc, expected, ended = [0, 0], {}, set()
        for _ in range(1000):
            progress = False
            for node, raw in enumerate(packages):
                if node in ended:
                    continue
                h = headers[node]
                word, arg = struct.unpack_from(
                    "<QQ", raw, h[A.KA_PH_OFF_STEP] + pc[node] * 16
                )
                op, unit, count = word & 255, (word >> 16) & 65535, word >> 32
                if op == A.KA_OP_DISPATCH:
                    for i in range(count):
                        self.execute(
                            node,
                            struct.unpack_from(
                                "<4Q", raw, h[A.KA_PH_OFF_PAY] + 32 * (arg + i)
                            ),
                        )
                elif op == A.KA_OP_AWAIT:
                    key = (node, pc[node])
                    if key not in expected:
                        expected[key] = (
                            sum(
                                amount
                                for (owner, _), amount in expected.items()
                                if owner == node
                            )
                            + count
                        )
                    if self.counts[node] < expected[key]:
                        continue
                elif op == A.KA_OP_WAIT_SLOT:
                    assert unit == 9 and (word >> 8) & A.KA_STEP_F_CONSUME
                    if self.slots[node] < count:
                        continue
                    self.slots[node] -= count
                elif op == A.KA_OP_ENGINE:
                    for i in range(count):
                        group = struct.unpack_from(
                            "<4Q", raw, h[A.KA_PH_OFF_PAY] + 32 * (arg + i // 3)
                        )
                        code = (group[0] >> (8 * (i % 3))) & 255
                        assert code == A.KOS_DE_IL_RING
                        value = group[i % 3 + 1]
                        assert (value >> 8) & 255 == 9
                        self.slots[value & 3] += ((value >> 16) & 65535) or 1
                elif op == A.KA_OP_RING:
                    assert count == 9
                    self.slots[unit] += (arg & 65535) or 1
                else:
                    assert op == A.KA_OP_END
                    ended.add(node)
                pc[node] += 1
                progress = True
            if len(ended) == len(packages):
                return self.finish()
            assert progress, "firmware package graph cannot progress"
        raise AssertionError("firmware graph did not terminate")


class Memory(HostMemory):
    next_address = 1 << 22

    def spill(self, size):
        address = self.next_address
        self.next_address += (size + 255) // 256 * 256
        return address


class FirmwarePair:
    def __init__(self):
        self.layout = Layout.from_machine(MACHINE)
        self.queues, self.firmware = {}, []
        self.engine = Engine()
        self.entered = set()
        self.packages = []
        for node in range(2):
            mem = Memory()
            queue = NodeQueue(
                mem, 0, layout=self.layout, record=self.layout.record, spill=mem.spill
            )
            queue.init()
            fw = SpecFirmware(
                mem,
                0,
                interp=Interpreter(self, node),
                layout=self.layout,
                record=self.layout.record,
            )
            fw.boot()
            queue.idle = self.progress
            self.queues[node] = queue
            self.firmware.append(fw)

    def progress(self):
        for fw in self.firmware:
            fw.step()

    def execute(self, node, raw):
        if not self.entered:
            packages = []
            for fw in self.firmware:
                head = fw.rd(self.layout.sq_head)
                assert head < fw.rd(self.layout.sq_tail), "not every CPU has its part"
                outer = fw.entry(head)
                item = [
                    fw.m.read64(outer[self.layout.list_addr] + 8 * k)
                    for k in range(self.layout.sq_words)
                ]
                package, bindings = fw.read_run(item)
                assert not bindings
                packages.append(package)
            self.packages = packages
            self.engine.firmware(packages)
        assert raw == self.packages[node]
        self.entered.add(node)
        if len(self.entered) == 2:
            self.entered.clear()
        return SimpleNamespace(status=self.layout.ok, sent=1, time={})


class Interpreter:
    def __init__(self, pair, node):
        self.pair, self.node = pair, node

    def run(self, raw, bindings):
        assert not bindings
        return self.pair.execute(self.node, raw)


def test_package_parts_preserves_peer_frontiers_across_leased_slot_edges():
    bound = artifact()
    graphs = Graph.package_parts(bound, {(0, 1, 2): 9, (1, 1, 2): 27}, de_fetch=False)
    kaos = Engine()
    firmware = FirmwarePair()
    retained = PreparedFirmwareGraph(bound, firmware.queues, SlotLeases())
    expected = struct.pack("<QQ", 0x123456789ABCDEF0, 0x123456789ABCDEF0 ^ 0x55AA)
    for _ in range(2):
        kaos.output[:] = b"\xcc" * 16
        firmware.engine.output[:] = b"\xcc" * 16
        assert kaos.run(graphs) == expected
        assert all(c.ok for c in retained.run().values())
        assert bytes(firmware.engine.output) == expected
        assert firmware.packages == [part.package for part in bound.parts]
    retained.close()
