"""Publication ordering, ring wrap, CQ snapshot and bounded resident cache."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.client import RecordSkew
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport

BASE = 0x100000
RECORD = L.Record(1, 128, {"tag": (0, 8), "so_wr": (8, 8), "idle": (96, 8)})


class Interpreter:
    def __init__(self):
        self.seen = []

    def run(self, pkg, binds):
        self.seen.append((pkg, binds))
        return SimpleNamespace(status=0, sent=len(pkg))


def queue(batch=4, bulk=True, cache=128):
    mem, freed, allocation = MemoryTransport(), [], [0x800000]

    def alloc(n):
        at = allocation[0]
        allocation[0] += n
        return at

    q = NodeQueue(
        mem, BASE, sq_n=4, cq_n=4, record=RECORD, spill=alloc, unspill=freed.append
    )
    q.batch_size, q.cq_bulk, q.resident_bytes = batch, bulk, cache
    q.init()
    interp = Interpreter()
    fw = SpecFirmware(
        mem,
        BASE,
        interp,
        record={"bytes": 128, "fields": {k: v[0] for k, v in RECORD.fields.items()}},
    )
    fw.boot()
    q.idle = fw.step
    return mem, q, fw, interp, freed


def test_publication_after_all_entries_and_bindings():
    mem, q, fw, interp, _ = queue()
    original = mem.write_block
    observed = []

    def write(addr, raw):
        original(addr, raw)
        if addr == BASE + L.SQ_TAIL:
            while fw.rd(L.SQ_HEAD) != fw.rd(L.SQ_TAIL):
                fw.step()
            observed.extend(interp.seen)

    with patch.object(mem, "write_block", write):
        tags = [q.submit(bytes([i]) * 32, [0x1000 + i]) for i in range(4)]
    assert observed == [(bytes([i]) * 32, [0x1000 + i]) for i in range(4)]
    assert [q.wait(t).tag for t in tags] == tags


def test_early_tail_mutant_is_caught(monkeypatch):
    original = NodeQueue.flush

    def early(self):
        if self._staged:
            self.mem.write_block(self.base + L.SQ_TAIL, L.line(self.sq_tail))
        return original(self)

    monkeypatch.setattr(NodeQueue, "flush", early)
    with pytest.raises(AssertionError):
        test_publication_after_all_entries_and_bindings()


@pytest.mark.parametrize("bulk", [False, True])
def test_partial_batch_wrap_and_completion_bytes(bulk):
    _, q, fw, interp, _ = queue(bulk=bulk)
    for n in (3, 4, 1, 4, 2):
        tags = [q.submit(bytes([i]) * 32, [i]) for i in range(n)]
        q.flush()
        for _ in tags:
            fw.step()
        got = q.poll()
        assert [c.tag for c in got] == tags
        assert all(c.record["tag"] == c.tag for c in got)
        for t in tags:
            assert q.wait(t).ok
    assert len(interp.seen) == 14


def test_bulk_reads_one_snapshot_and_one_ack():
    mem, q, fw, _, _ = queue()
    tags = [q.submit(bytes([i]) * 32) for i in range(4)]
    for _ in tags:
        fw.step()
    # Discard stdout to isolate the completion protocol.
    q.read_stdout()
    mem.blocks.clear()
    got = q.poll()
    assert len(got) == 4
    assert mem.blocks == [(BASE + L.FW, q.geo["end"] - L.FW), (BASE + L.CQ_HEAD, 64)]


def test_missing_completion_mutant_is_caught(monkeypatch):
    original = NodeQueue.poll

    def skip(self):
        return original(self)[:-1]

    monkeypatch.setattr(NodeQueue, "poll", skip)
    with pytest.raises(AssertionError):
        test_partial_batch_wrap_and_completion_bytes(True)


def test_bad_record_never_partially_retires_snapshot():
    mem, q, fw, _, freed = queue(cache=0)
    tags = [q.submit(bytes([i]) * 32) for i in range(4)]
    for _ in tags:
        fw.step()
    mem.write64(BASE + q.geo["rec"] + 3 * 128, 999)
    with pytest.raises(RecordSkew):
        q.poll()
    assert q.cq_head == 0 and not freed and not q._done


def test_duplicate_final_never_masks_earlier_failure():
    mem, q, fw, _, freed = queue(cache=0)
    tags = [q.submit(bytes([i]) * 32) for i in range(4)]
    for _ in tags:
        fw.step()
    mem.write64(BASE + q.geo["cq"] + L.CQ_BYTES, tags[0])
    mem.write64(BASE + q.geo["rec"] + L.REC_BYTES, tags[0])
    with pytest.raises(RecordSkew, match="stale"):
        q.poll()
    assert q.cq_head == 0 and not q._done and not freed


def test_duplicate_guard_mutant_is_caught(monkeypatch):
    class MissingSet(set):
        def add(self, key):
            pass

    monkeypatch.setattr(
        "kohakuaccel.driver.node.queue.client.set",
        lambda *args: MissingSet(*args) if args else set(),
        raising=False,
    )
    with pytest.raises(pytest.fail.Exception):
        test_duplicate_final_never_masks_earlier_failure()


def test_resident_eviction_reset_and_current_bytes():
    _, q, fw, interp, freed = queue(batch=1, cache=64)
    for raw in (b"a" * 32, b"a" * 32, b"b" * 32, b"a" * 32):
        tag = q.submit(raw)
        fw.step()
        q.wait(tag)
    assert [p for p, _ in interp.seen] == [b"a" * 32, b"a" * 32, b"b" * 32, b"a" * 32]
    assert q.counters["uploads"] == 3 and q.counters["cache_hits"] == 1
    assert len(freed) == 2
    q.init()
    assert len(freed) == 3 and not q._cache


def test_stale_cache_after_free_mutant_is_caught(monkeypatch):
    original = NodeQueue._evict_package

    def stale(self, key):
        cached = self._cache.get(key)
        original(self, key)
        if cached is not None:
            self._cache[key] = cached

    monkeypatch.setattr(NodeQueue, "_evict_package", stale)
    with pytest.raises(AssertionError):
        test_resident_eviction_reset_and_current_bytes()


def test_failed_tail_poisoned_until_reset():
    mem, q, _, _, freed = queue()
    original = mem.write_block

    def fail(addr, data):
        if addr == BASE + L.SQ_TAIL:
            raise OSError("ambiguous tail")
        original(addr, data)

    with patch.object(mem, "write_block", fail), pytest.raises(OSError):
        for _ in range(4):
            q.submit(b"a" * 32)
    assert not freed and len(q._outstanding) == 4
    with pytest.raises(RuntimeError, match="reset"):
        q.submit(b"b" * 32)
    q.init()
    assert not q._broken and len(freed) == 1


def test_overdeep_batch_refused(monkeypatch):
    monkeypatch.setenv("KOHAKUTPU_SUBMIT_BATCH", "5")
    with pytest.raises(ValueError, match="rings"):
        queue()


def test_backoff_applies_during_spin(monkeypatch):
    _, q, fw, _, _ = queue(batch=1)
    q.spin = 1
    q.backoff = 0.0001
    slept = []

    def sleep(delay):
        slept.append(delay)
        fw.step()

    monkeypatch.setattr("kohakuaccel.driver.node.queue.client.time.sleep", sleep)
    tag = q.submit(b"a" * 32)
    q.wait(tag)
    assert slept == [0.00001]


def test_skipped_backoff_mutant_is_caught(monkeypatch):
    original = NodeQueue.wait

    def no_backoff(self, *args, **kw):
        self.backoff = 0
        return original(self, *args, **kw)

    monkeypatch.setattr(NodeQueue, "wait", no_backoff)
    with pytest.raises(AssertionError):
        test_backoff_is_bounded_and_never_used_on_submit(monkeypatch)


def test_backoff_is_bounded_and_never_used_on_submit(monkeypatch):
    _, q, fw, _, _ = queue(batch=1)
    q.backoff = 0.00004
    slept = []
    monkeypatch.setattr("kohakuaccel.driver.node.queue.client.time.sleep", slept.append)
    tag = q.submit(b"a" * 32)
    assert not slept
    ticks = [0]

    def idle():
        ticks[0] += 1
        if ticks[0] == 5:
            fw.step()

    q.idle = idle
    q.wait(tag)
    assert slept == [0.00001, 0.00002, 0.00004, 0.00004, 0.00004]
