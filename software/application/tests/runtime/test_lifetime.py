"""Allocation ownership and submission references are independent lifetimes."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime.arena import Arena, OutOfMemory
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.packages import Prebuilt
from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF

MACHINE = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
PACKAGE = _rs.package_build('{"credit": 32}', "[]", MACHINE, defaults=False)


class Queue:
    def __init__(self):
        self.submitted = 0
        self.collected = []
        self.fail = False

    def submit(self, program):
        self.submitted += 1
        return self.submitted

    def collect(self, pending):
        if self.fail:
            raise TimeoutError("not completed")
        self.collected.append(pending)
        return {"cycles": pending}

    def drop_spilled(self):
        pass


def kernel(scratch=0):
    return SimpleNamespace(
        name="lifetime",
        l1=SimpleNamespace(
            body=lambda *args: SimpleNamespace(params=[]), params=lambda *args: []
        ),
        scratch=[{"space": "dram", "bytes": scratch}] if scratch else [],
        programs=lambda binds: {
            None: Prebuilt(PACKAGE, (), MACHINE, 0, (), False, None)
        },
    )


def device():
    dev = Device(kind="offline", board="multimesh_v9t6")
    dev.card = Queue()
    dev.max_inflight = None
    return dev


def test_owner_free_waits_for_every_submission_and_view():
    dev = device()
    at = dev.alloc(1024)
    a = dev.submit(kernel(), {"input": at, "alias": at + 32})
    b = dev.submit(kernel(), {"output": at + 64})
    assert dev.dram.references[dev.mmu.entries[at].physical] == 2
    dev.free(at)
    assert dev.mmu.entries[at].live
    dev.collect(a)
    assert dev.mmu.entries[at].live
    dev.collect(b)
    assert not dev.mmu.entries[at].live
    dev.collect(a)
    assert dev.card.collected == [1, 2]


def test_failed_wait_keeps_scratch_and_dependencies_until_completion():
    dev = device()
    at = dev.alloc(1024)
    pending = dev.submit(kernel(1024), {"x": at})
    dev.free(at)
    used = dev.dram.used
    dev.card.fail = True
    with pytest.raises(TimeoutError):
        dev.collect(pending)
    assert dev.dram.used == used
    assert dev.inflight == [pending]
    window = dev.mmu.resolve(dev.windows["dram", None][0])
    assert (window[0].arena, window[1]) in pending.references
    dev.card.fail = False
    dev.collect(pending)
    assert not dev.mmu.entries[at].live


def test_pressure_collects_only_as_many_oldest_runs_as_needed():
    dev = device()
    a = dev.submit(kernel(1024), {})
    b = dev.submit(kernel(1024), {})

    def take():
        if a.run is None:
            raise OutOfMemory()
        return 123

    assert dev._take(take) == 123
    assert dev.card.collected == [1]
    assert dev.inflight == [b]
    dev.drain()


def test_released_owner_cannot_submit_again():
    dev = device()
    at = dev.alloc(1024)
    pending = dev.submit(kernel(), {"x": at})
    dev.free(at)
    with pytest.raises(ValueError, match="live owner"):
        dev.submit(kernel(), {"x": at})
    dev.collect(pending)


def test_failed_compile_rolls_back_retains_and_scratch():
    dev = device()
    at = dev.alloc(1024)
    used = dev.dram.used
    k = kernel(1024)

    def fail(binds):
        raise ValueError("compile")

    k.programs = fail
    with pytest.raises(ValueError, match="compile"):
        dev.submit(k, {"x": at})
    # With no run in flight the scratch window goes with the failed submission.
    assert not dev.windows and dev.dram.used == used
    assert dev.dram.references[dev.mmu.entries[at].physical] == 0
    dev.free(at)


def test_partial_publication_failure_quarantines_all_references():
    dev = device()
    at = dev.alloc(32)

    def partial(program):
        raise TimeoutError("one node may be running")

    dev.card.submit = partial
    with pytest.raises(TimeoutError):
        dev.submit(kernel(1024), {"x": at})
    dev.free(at)
    assert dev.mmu.entries[at].live and dev._uncertain[0].references
    with pytest.raises(RuntimeError, match="publication"):
        dev.submit(kernel(), {})


def test_owner_usage_peak_and_fragmentation():
    arena = Arena(0, 1024, 32)
    a = arena.alloc(33, owner="input")
    b = arena.alloc(64, owner="scratch")
    c = arena.alloc(64, owner="output")
    arena.release(b)
    stats = arena.stats()
    assert stats == {
        "used": 128,
        "requested": 97,
        "peak": 192,
        "allocations": 3,
        "free": 896,
        "largest": 832,
        "free_spans": 2,
        "fragmented": 64,
        "owners": {"input": 64, "output": 64},
    }
    arena.release(a)
    arena.release(c)
    assert arena.free == arena.largest == 1024


def test_recording_pins_main_heap_until_log_release():
    dev = Device(kind="offline", board="multimesh_v9t6")
    assert dev.dram.used == 0 and not dev.private
    at = dev.alloc(32)
    dev.record()
    dev.write(at, b"x" * 32)
    dev.free(at)
    log = dev.recorded()
    assert dev.mmu.entries[at].live
    other = dev.alloc(32)
    assert other != at
    dev.replay(log)
    assert dev.read(at, 32) == b"x" * 32
    log.close()
    log.close()
    assert not dev.mmu.entries[at].live
    with pytest.raises(ValueError, match="pinned"):
        dev.replay(log)
    dev.free(other)
    dev.close()


def test_failed_replay_keeps_independent_references_when_log_closes():
    dev = Device(kind="offline", board="multimesh_v9t6")
    at = dev.alloc(32)
    dev.record()
    dev.write(at, bytes(32))
    dev.free(at)
    log = dev.recorded()

    def fail(events):
        dev.card._publishing()
        raise TimeoutError("replay may be running")

    dev.card.replay = fail
    with pytest.raises(TimeoutError):
        dev.replay(log)
    log.close()
    assert dev.mmu.entries[at].live
    assert dev.alloc(32) != at


def test_replay_reference_fix_has_a_reversal_witness(monkeypatch):
    monkeypatch.setattr(Arena, "retain_pinned", lambda *args: None)
    with pytest.raises(AssertionError):
        test_failed_replay_keeps_independent_references_when_log_closes()


def test_recording_refuses_another_device():
    first = Device(kind="offline", board="multimesh_v9t6")
    other = Device(kind="offline", board="multimesh_v9t6")
    first.record()
    at = first.alloc(32)
    first.write(at, bytes(32))
    log = first.recorded()
    with pytest.raises(ValueError, match="pinned"):
        other.replay(log)
    first.free(at)
    first.close()
    log.close()
    assert first.dram.used == 0
    assert first.close() == {}
    other.close()


def test_teardown_revokes_pinned_spans_after_stop():
    dev = Device(kind="offline", board="multimesh_v9t6")
    dev.record()
    at = dev.alloc(32)
    dev.write(at, bytes(32))
    log = dev.recorded()
    dev.free(at)
    assert dev.dram.used > 0
    dev.close()
    assert dev.dram.used == 0
    log.close()


def test_teardown_reversal(monkeypatch):
    monkeypatch.setattr(Arena, "close", lambda self: None)
    with pytest.raises(AssertionError):
        test_teardown_revokes_pinned_spans_after_stop()


def test_recording_fix_has_a_reversal_witness(monkeypatch):
    monkeypatch.setattr(Device, "_pin_record", lambda *args: None)
    with pytest.raises(AssertionError):
        test_recording_pins_main_heap_until_log_release()


def test_reference_fix_has_a_reversal_witness(monkeypatch):
    def immediate(self, at):
        self.released.add(at)
        self._release(at)

    monkeypatch.setattr(Arena, "release", immediate)
    with pytest.raises(AssertionError):
        test_owner_free_waits_for_every_submission_and_view()


def test_failed_wait_fix_has_a_reversal_witness(monkeypatch):
    collect = Device.collect

    def unsafe(self, pending):
        try:
            return collect(self, pending)
        except TimeoutError:
            for arena, at in pending.references:
                arena.unretain(at)
            raise

    monkeypatch.setattr(Device, "collect", unsafe)
    with pytest.raises(AssertionError):
        test_failed_wait_keeps_scratch_and_dependencies_until_completion()


def test_pressure_fix_has_a_reversal_witness(monkeypatch):
    def drain_all(self, alloc):
        try:
            return alloc()
        except OutOfMemory:
            self.drain()
            return alloc()

    monkeypatch.setattr(Device, "_take", drain_all)
    with pytest.raises(AssertionError):
        test_pressure_collects_only_as_many_oldest_runs_as_needed()
