"""Counting queue backend: real host protocol, non-numeric firmware double.

Calls are unit-global read/write operations (one XDMA transfer below its ceiling),
not Verilator pipe commands. Firmware accesses bypass the counting wrapper.
"""

from collections import defaultdict

from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakutpu.application.runtime.card import NodeCard, node_queue
from kohakutpu.application.runtime.offline import OfflineCard


class CountingMemory:
    def __init__(self, inner, memory):
        self.inner = inner
        self.memory = memory
        self.queues = {}
        self.counts = defaultdict(lambda: [0, 0])

    def purpose(self, addr, nbytes, write):
        for q in self.queues.values():
            off = addr - q.base
            if off == L.FW and not write and nbytes > L.SQ:
                return "cq_snapshot"
            fixed = {
                L.SQ_TAIL: "sq_tail",
                L.SQ_HEAD: "sq_head",
                L.CQ_TAIL: "cq_poll",
                L.CQ_HEAD: "cq_ack",
                L.SO_WR: "stdout",
                L.SO_RD: "stdout",
                L.FW: "firmware_state",
            }
            if off in fixed:
                return fixed[off]
            if q.geo["sq"] <= off < q.geo["cq"]:
                return "sq_entries"
            if q.geo["cq"] <= off < q.geo["so"]:
                return "cq_entries"
            if q.geo["so"] <= off < q.geo["si"]:
                return "stdout"
            if q.geo["rec"] <= off < q.geo["end"]:
                return "cq_records"
            if q.bind_off <= off < q.heap_off:
                return "bindings"
        if self.memory.dram.owners.get(addr) == "package":
            return "package_upload"
        return "tensor_write" if write else "tensor_read"

    def read_block(self, addr, nbytes):
        key = self.purpose(addr, nbytes, False)
        self.counts[key][0] += 1
        self.counts[key][1] += nbytes
        return self.inner.read_block(addr, nbytes)

    def write_block(self, addr, data):
        key = self.purpose(addr, len(data), True)
        self.counts[key][0] += 1
        self.counts[key][1] += len(data)
        self.inner.write_block(addr, data)

    def read64(self, addr):
        return int.from_bytes(self.read_block(addr, 8), "little")

    def write64(self, addr, value):
        self.write_block(addr, value.to_bytes(8, "little"))

    def snapshot(self):
        return {k: list(v) for k, v in self.counts.items()}


class QuietFirmware(SpecFirmware):
    def print(self, text):
        pass


class CountingCard(OfflineCard):
    """Completes published work at idle, without predicting tensor values."""

    submit = NodeCard.submit
    _submit = NodeCard._submit
    collect = NodeCard.collect
    _collect = NodeCard._collect
    _result = NodeCard._result
    _segment = NodeCard._segment
    _segment_pkgt = NodeCard._segment_pkgt
    submit_recorded = NodeCard.submit_recorded
    prepare_recorded = NodeCard.prepare_recorded
    submit_prepared = NodeCard.submit_prepared
    record_layout = NodeCard.record_layout

    def __init__(self, board, nodes=(0,)):
        super().__init__(board, nodes)
        self.mem = CountingMemory(self.mem, self.memory)
        self.queues = {}
        self.firmware = {}
        self._order, self._stdout, self._segs = {}, {}, {}
        self.timeout = 1
        self.debug = False
        for n in nodes:
            base = self.memory.staging[n].lo - (1 << 20)
            q = node_queue(self.mem, base, self.memory, record=self.record_layout(4))
            q.init()
            q.track_ends = True
            schema = {
                "bytes": L.REC_BYTES,
                "fields": {k: v[0] for k, v in q.record.fields.items()},
            }
            fw = QuietFirmware(self.mem.inner, base, record=schema)
            fw.boot()
            self.queues[n], self.firmware[n] = q, fw
            q.idle = lambda n=n: self.advance(n)
        self.mem.queues = self.queues
        self.q = self.queues[nodes[0]]

    def get(self, at, nbytes):
        """Count the real read, but never interpret stale package bytes as tensors."""
        super().get(at, nbytes)
        return bytes(nbytes)

    def advance(self, node):
        q, fw = self.queues[node], self.firmware[node]
        while fw.rd(L.SQ_HEAD) != fw.rd(L.SQ_TAIL):
            if fw.rd(L.CQ_TAIL) - fw.rd(L.CQ_HEAD) >= q.cq_n:
                break
            fw.step()

    def drop_spilled(self):
        for q in self.queues.values():
            q.drop_spilled()
