"""Native RUN publication keeps entry bytes, wrapping, and failure ownership."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs

MACHINE = Path(__file__).resolve().parents[4] / "machines" / "multimesh_v9t6.json"
LAYOUT = L.Layout.from_machine(MACHINE)


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
    q = NodeQueue(mem, 0, sq_n=2, cq_n=4, head_cache=True, layout=LAYOUT)
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


def test_native_queue_bytes_and_ring_wrap_match_python():
    assert transcript(True) == transcript(False)
    # QueueRun holds _rs.QueueLayout at construction and does not rebuild from Python on submit.
    mem = Memory()
    q = NodeQueue(mem, 0, sq_n=2, cq_n=4, head_cache=True, layout=LAYOUT)
    q.init()
    runner = _rs.QueueRun(LAYOUT)
    assert isinstance(runner.layout, _rs.QueueLayout)
    q.native_run = runner

    class PoisonedLayout:
        op_run = LAYOUT.op_run
        line = LAYOUT.line

        @property
        def schema(self):
            raise AssertionError("QueueRun rebuilt QueueLayout on hot path")

    q.layout = PoisonedLayout()
    assert q.submit(b"protocol double".ljust(32, b"\0")) == 1
    with pytest.raises(AssertionError, match="hot path"):
        _rs.QueueLayout(q.layout)
    with pytest.raises(TypeError):
        _rs.queue_run(SimpleNamespace(_broken=False), b"pkg", [])
    with pytest.raises(TypeError):
        _rs.queue_run(SimpleNamespace(_broken=False), b"pkg", [], None)


def failure_witness(native):
    mem = Memory()
    q = NodeQueue(mem, 0, sq_n=2, cq_n=4, layout=LAYOUT)
    q.native_run = native
    q.init()
    mem.fail_at = q.geo["sq"]
    with pytest.raises(OSError, match="publication"):
        q.submit(b"package".ljust(32, b"\0"))
    assert q._broken and q._outstanding == {1}, "published-failure ownership was lost"
    assert 1 in q._package_tags


def test_native_queue_failed_publication_keeps_pins():
    failure_witness(_rs.QueueRun(LAYOUT))


def test_mutant_native_queue_failed_publication_retires_is_caught():
    assert "bindings.queue/retires_failed" in rs.mutants()
    with (
        rs.mutant("bindings.queue/retires_failed"),
        pytest.raises(AssertionError, match="ownership"),
    ):
        failure_witness(_rs.QueueRun(LAYOUT))
