"""Graph-owned package uploads outlive final completion, never queue reset."""

from pathlib import Path
from unittest.mock import patch

import pytest

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport

LAYOUT = L.Layout.from_machine(
    Path(__file__).resolve().parents[3] / "machines/multimesh_v9t6.json"
)


def queue():
    mem, freed, used = MemoryTransport(), [], []

    def allocate(n):
        at = 0x800000 + sum(used)
        used.append(n)
        return at

    q = NodeQueue(mem, 0x100000, spill=allocate, unspill=freed.append, layout=LAYOUT)
    q.init()
    fw = SpecFirmware(mem, q.base, layout=LAYOUT)
    fw.boot()
    return q, fw, freed


def test_prepare_uploads_once_and_retains_through_last_submission():
    q, fw, freed = queue()
    raw = b"a" * 32
    prepared = q.prepare(raw)
    for _ in range(3):
        tag = q.submit(prepared)
        fw.step()
        assert q.wait(tag).ok
        assert not freed
        assert q.mem.read_block(prepared.address, 32) == raw
    assert q.counters["uploads"] == 1
    tag = q.submit(prepared)
    prepared.close()
    prepared.close()
    assert not freed
    fw.step()
    q.wait(tag)
    assert freed == [prepared.address] and not q._cache


def test_shared_pin_and_eviction():
    q, _, freed = queue()
    a = q.prepare(b"a" * 32)
    b = q.prepare(b"a" * 32)
    q.drop_spilled()
    assert not freed and a.address == b.address
    a.close()
    assert not freed
    b.close()
    assert freed == [a.address]


def test_reset_rejects_old_upload_and_old_close_cannot_release_new_pin():
    q, _, freed = queue()
    a = q.prepare(b"a" * 32)
    q.init()
    b = q.prepare(b"a" * 32)
    a.close()
    assert q._package_pins[b.key] == 1
    with pytest.raises(ValueError, match="epoch"):
        q.submit(a)
    b.close()
    assert freed == [a.address, b.address]


def test_foreign_and_closed_uploads_refuse():
    q, _, _ = queue()
    other, _, _ = queue()
    a = q.prepare(b"a" * 32)
    with pytest.raises(ValueError, match="epoch"):
        other.submit(a)
    a.close()
    with pytest.raises(ValueError, match="epoch"):
        q.submit(a)


def test_failed_publication_keeps_upload_after_graph_close():
    q, _, freed = queue()
    a = q.prepare(b"a" * 32)
    with (
        patch.object(q.mem, "write_block", side_effect=OSError("publication")),
        pytest.raises(OSError),
    ):
        q.submit(a)
    a.close()
    assert not freed and q._package_tags
    q.init()
    assert freed == [a.address]


def test_skipped_pin_mutant_is_caught(monkeypatch):
    original = NodeQueue.prepare

    def unpinned(self, raw):
        got = original(self, raw)
        self._package_pins.clear()
        return got

    monkeypatch.setattr(NodeQueue, "prepare", unpinned)
    with pytest.raises(ValueError, match="epoch"):
        test_prepare_uploads_once_and_retains_through_last_submission()


def test_skipped_completion_mutant_is_caught(monkeypatch):
    monkeypatch.setattr(NodeQueue, "_retire_package", lambda *a: None)
    with pytest.raises(AssertionError):
        test_prepare_uploads_once_and_retains_through_last_submission()


def test_wrong_address_mutant_is_caught(monkeypatch):
    original = NodeQueue.prepare

    def wrong(self, raw):
        got = original(self, raw)
        got.address += 64
        return got

    monkeypatch.setattr(NodeQueue, "prepare", wrong)
    with pytest.raises(AssertionError):
        test_prepare_uploads_once_and_retains_through_last_submission()
