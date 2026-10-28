"""Shared submissions hold one package until its final completion."""

import pytest

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF


def package():
    text = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    return _rs.package_build('{"credit": 32}', "[]", text, defaults=False)


def test_shared_package_retires_at_final_completion():
    mem = MemoryTransport()
    freed = []
    q = NodeQueue(mem, 0x100000, spill=lambda n: 0x800000, unspill=freed.append)
    q.init()
    fw = SpecFirmware(mem, q.base)
    fw.boot()
    raw = package()
    a, b = q.submit(raw), q.submit(raw)
    assert len(q._spilled) == 1 and not freed
    fw.step()
    q.poll()
    assert not freed
    fw.step()
    q.poll()
    assert freed == [0x800000]
    assert not q._cache and not q._spilled
    q.wait(a)
    q.wait(b)
    assert freed == [0x800000]


def test_failed_upload_returns_spill(monkeypatch):
    mem, freed = MemoryTransport(), []
    q = NodeQueue(mem, 0x100000, spill=lambda n: 0x800000, unspill=freed.append)
    q.init()

    def fail(*args):
        raise OSError("transport write failed")

    monkeypatch.setattr(mem, "write_block", fail)
    with pytest.raises(OSError):
        q.upload(package())
    assert freed == [0x800000]
    assert not q._cache and not q._spilled


def test_failed_recycled_upload_keeps_free_span(monkeypatch):
    mem = MemoryTransport()
    q = NodeQueue(mem, 0x100000)
    q.init()
    q._heap_free = [(q.base + q.heap_off, 256)]
    before = list(q._heap_free)

    def fail(*args):
        raise OSError("transport write failed")

    monkeypatch.setattr(mem, "write_block", fail)
    with pytest.raises(OSError):
        q.upload(package())
    assert q._heap_free == before


def test_upload_checks_padded_extent():
    mem = MemoryTransport()
    q = NodeQueue(mem, 0x100000)
    q.init()
    raw = package()
    assert len(raw) % 64
    q.size = q.heap_off + len(raw)
    with pytest.raises(ValueError, match="exceeds"):
        q.upload(raw)


def test_retirement_reversal(monkeypatch):
    monkeypatch.setattr(NodeQueue, "_retire_package", lambda *args: None)
    with pytest.raises(AssertionError):
        test_shared_package_retires_at_final_completion()
