"""The native queue: its RUN bytes, its failed publications, its poll's wake conditions."""

import os
import struct
import tempfile
from pathlib import Path

import pytest

from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.stack.rs import _rs

MACHINE = Path(__file__).resolve().parents[3] / "machines" / "multimesh_v9t6.json"
LAYOUT = L.Layout.from_machine(MACHINE)
NATIVE = _rs.QueueLayout(LAYOUT)


class Memory(HostMemory):
    def __init__(self):
        super().__init__()
        self.writes = []
        self.fail_at = None

    def write_block(self, at, data):
        if at == self.fail_at:
            raise OSError("publication transport failure")
        self.writes.append((at, bytes(data)))
        return super().write_block(at, data)


def transcript(native):
    mem = Memory()
    q = NodeQueue(mem, 0, sq_n=2, cq_n=4, layout=LAYOUT)
    q.native_run = _rs.QueueRun(LAYOUT) if native else None
    q.init()
    fw = SpecFirmware(mem, 0, layout=LAYOUT)
    fw.boot()
    q.idle = fw.step
    mem.writes.clear()
    for i in range(5):
        tag = q.submit(b"protocol double".ljust(32, b"\0"), [0x1000 + i * 64, 0])
        assert q.wait(tag).ok
    return mem.writes, q.counters


def test_native_run_bytes_and_ring_wrap_are_python_s():
    assert transcript(True) == transcript(False)


def test_a_failed_publication_keeps_its_pins():
    mem = Memory()
    q = NodeQueue(mem, 0, sq_n=2, cq_n=4, layout=LAYOUT)
    q.native_run = _rs.QueueRun(LAYOUT)
    q.init()
    mem.fail_at = q.geo["sq"]
    with pytest.raises(OSError, match="publication"):
        q.submit(b"package".ljust(32, b"\0"))
        q.flush()
    assert q._broken and q._outstanding == {1}
    assert 1 in q._package_tags


def test_fatal_and_stdout_wake_the_poll():
    with tempfile.TemporaryFile() as memory:
        raw = bytearray(384)
        struct.pack_into("<Q", raw, 0, NATIVE.st_fatal)
        struct.pack_into("<Q", raw, 24, 0x103)
        memory.write(raw)
        memory.flush()
        args = (memory.fileno(), 0, 0, 0, 0.002, 0.00001, 0, NATIVE)
        assert _rs.wait_tail(*args) == (bytes(raw), 1)
        struct.pack_into("<Q", raw, 0, NATIVE.st_ready)
        struct.pack_into("<Q", raw, 320, 64)
        os.pwrite(memory.fileno(), raw, 0)
        assert _rs.wait_tail(*args) == (bytes(raw), 1)
