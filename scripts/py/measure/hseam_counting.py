"""Counting queue backend: real host protocol, non-numeric firmware double.

Calls are unit-global read/write operations (one XDMA transfer below its ceiling),
not Verilator pipe commands. Firmware accesses bypass the counting wrapper.
"""

import os
from collections import defaultdict

from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakutpu.application.runtime.card import NodeCard, node_queue
from kohakutpu.application.runtime.offline import OfflineCard
from kohakutpu.compiler import _rs


class CountingMemory:
    def __init__(self, inner, memory):
        self.inner = inner
        self.memory = memory
        self.queues = {}
        self.counts = defaultdict(lambda: [0, 0])

    def purpose(self, addr, nbytes, write):
        for q in self.queues.values():
            layout = q.layout
            off = addr - q.base
            if off == layout.fw and not write and nbytes > layout.sq:
                return "cq_snapshot"
            fixed = {
                layout.sq_tail: "sq_tail",
                layout.sq_head: "sq_head",
                layout.cq_tail: "cq_poll",
                layout.cq_head: "cq_ack",
                layout.so_wr: "stdout",
                layout.so_rd: "stdout",
                layout.fw: "firmware_state",
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
    submit_wire = NodeCard.submit_wire
    _submit = NodeCard._submit
    collect = NodeCard.collect
    _collect = NodeCard._collect
    _result = NodeCard._result
    _segment = NodeCard._segment
    _segment_pkgt = NodeCard._segment_pkgt
    submit_recorded = NodeCard.submit_recorded
    prepare_recorded = NodeCard.prepare_recorded
    submit_prepared = NodeCard.submit_prepared
    prepare_execution = NodeCard.prepare_execution
    submit_execution = NodeCard.submit_execution
    record_layout = NodeCard.record_layout

    def __init__(self, board, nodes=(0,), resident_mode=None):
        super().__init__(board, nodes, resident_mode=resident_mode)
        self.mem = CountingMemory(self.mem, self.memory)
        self.queues = {}
        self.firmware = {}
        self._order, self._stdout, self._segs = {}, {}, {}
        self.timeout = 1
        self.debug = False
        self.submit_cache = os.environ.get("KOHAKUTPU_HOST_SUBMIT_CACHE", "1") == "1"
        native_run = (
            _rs.QueueRun(self.queue_layout)
            if os.environ.get("KTPU_NATIVE_QUEUE", "0") == "1"
            else None
        )
        for n in nodes:
            base = self.memory.staging[n].lo - (1 << 20)
            q = node_queue(
                self.mem,
                base,
                self.memory,
                record=self.record_layout(self.queue_layout.version),
                layout=self.queue_layout,
            )
            q.native_run = native_run
            q.init()
            q.track_ends = True
            schema = {
                "bytes": self.queue_layout.rec_bytes,
                "fields": {k: v[0] for k, v in q.record.fields.items()},
            }
            fw = QuietFirmware(
                self.mem.inner, base, record=schema, layout=self.queue_layout
            )
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
        layout = q.layout
        while fw.rd(layout.sq_head) != fw.rd(layout.sq_tail):
            if fw.rd(layout.cq_tail) - fw.rd(layout.cq_head) >= q.cq_n:
                break
            fw.step()

    def drop_spilled(self):
        for q in self.queues.values():
            q.drop_spilled()
