"""Queued lifetime and ordering contracts; doubles do not predict card values."""

from types import SimpleNamespace

import pytest

from kohakuaccel.driver.node.queue import Completion, NodeError
from kohakutpu.application.ktpugrad import device as KD
from kohakutpu.application.runtime.arena import OutOfMemory
from kohakutpu.application.runtime.card import NodeCard, Pending
from kohakutpu.application.runtime.device import Device, Submitted
from kohakutpu.application.runtime.storage import Storage


def kernel(name="generated"):
    return SimpleNamespace(
        name=name,
        l1=SimpleNamespace(
            body=lambda *args: SimpleNamespace(params=[]), params=lambda *args: []
        ),
        scratch=[],
        programs=lambda binds: {None: dict(binds)},
    )


def device(depth=3, budget=None):
    dev = Device(kind="offline", board="vlt_v9t6s_1n", queue_bytes=budget)
    card = dev.card
    card.queues = {0: SimpleNamespace(sq_n=depth, cq_n=depth, flush=lambda: None)}
    card.submissions, card.collections = [], []
    card.fail = None

    def submit(program):
        card.submissions.append(program)
        if card.log is not None:
            card.log.append(("run", {0: (b"package", tuple(program.values()))}))
        return len(card.submissions)

    def collect(tag):
        card.collections.append(tag)
        if tag == card.fail:
            raise NodeError(Completion(tag, 0x20, 0, 7, 19, 0))
        return {"cycles": tag}

    card.submit, card.collect = submit, collect
    card.submit_recorded = lambda event: submit(dict(enumerate(event[0][1])))
    return dev


def launch(dev, source, dest, name="plan"):
    with dev.submission_scope(name, [source.addr]):
        return dev.run(kernel(), {"input": source.addr, "out": dest.addr})


def test_lazy_order_and_unrelated_host_access():
    dev = device()
    a, b, c, unrelated = [Storage(dev, 32) for _ in range(4)]
    first, second = launch(dev, a, b), launch(dev, b, c)
    assert isinstance(first, Submitted) and isinstance(second, Submitted)
    assert dev.card.collections == []
    dev.read(a.addr, 32)
    dev.write(unrelated.addr, bytes(32))
    assert dev.card.collections == []
    dev.read(b.addr, 32)
    assert dev.card.collections == [1] and dev.inflight == [second]
    dev.read(c.addr, 32)
    assert dev.card.collections == [1, 2]


def test_host_write_waits_for_reader_and_alias():
    dev = device()
    a, b = Storage(dev, 64), Storage(dev, 64)
    launch(dev, a, b)
    dev.write(a.addr + 32, bytes(32))
    assert dev.card.collections == [1]


def test_allocator_waits_for_side_form_reader():
    dev = device()
    a, b = Storage(dev, 32), Storage(dev, 32)
    side = a.form(("side",), 32)
    with dev.submission_scope("side", [side.addr]):
        dev.run(kernel(), {"input": side.addr, "out": b.addr})
    allocator = KD.KTPUAllocator(SimpleNamespace(rt=dev))
    allocator._copyin(a, memoryview(bytes(32)))
    assert dev.card.collections == [1]


def test_queue_depth_comes_from_layout():
    dev = device(depth=3)
    a, b = Storage(dev, 32), Storage(dev, 32)
    for _ in range(3):
        launch(dev, a, b)
    assert dev.card.collections == []
    launch(dev, a, b)
    assert dev.card.collections == [1] and len(dev.inflight) == 3


def test_byte_budget_counts_union_and_retires_oldest():
    dev = device(budget=512)
    a, b, c = [Storage(dev, 32) for _ in range(3)]
    launch(dev, a, b)
    launch(dev, a, b)
    assert not dev.card.collections
    launch(dev, a, c)
    assert dev.card.collections == [1, 2]
    dev.queue_bytes = 1
    with pytest.raises(OutOfMemory, match="one run"):
        launch(dev, a, b)
    assert len(dev.card.submissions) == 3


def test_free_after_last_completion():
    dev = device()
    a, b = Storage(dev, 32), Storage(dev, 32)
    first, second = launch(dev, a, b), launch(dev, a, b)
    address = a.addr
    physical = dev.mmu.entries[address].physical
    a.free()
    assert physical in dev.dram.live
    dev.collect(first)
    assert physical in dev.dram.live
    dev.collect(second)
    assert physical not in dev.dram.live


def test_fault_names_plan_run_completion_and_discards_later():
    dev = device()
    a, b = Storage(dev, 32), Storage(dev, 32)
    first = launch(dev, a, b, "first-plan")
    later = launch(dev, a, b, "later-plan")
    dev.card.fail = 1
    with pytest.raises(NodeError, match="first-plan; run generated.*tag 0x1") as caught:
        dev.collect(first)
    assert caught.value.completion.step == 7
    with pytest.raises(RuntimeError, match="later-plan.*discarded.*first-plan"):
        dev.collect(later)
    with pytest.raises(RuntimeError, match="discarded"):
        dev.read(b.addr, 32)
    a.free()
    assert dev.mmu.entries[a.addr].live
    assert dev.card.collections == [1]


def test_synchronize_and_explicit_timing_wait(monkeypatch):
    dev = device()
    a, b = Storage(dev, 32), Storage(dev, 32)
    program = object.__new__(KD.KTPUProgram)
    program.dev = SimpleNamespace(rt=dev, last_runs=[])
    program.plan = SimpleNamespace(name="tiny-plan", out_slot=1)
    program.slots = [0, 1]
    monkeypatch.setattr(
        KD.R,
        "execute",
        lambda rt, plan, bufs: [rt.run(kernel(), {"x": a.addr, "out": b.addr})],
    )
    assert program(a, b) is None
    assert not dev.card.collections
    assert program(a, b, wait=True) > 0
    assert dev.card.collections == [1, 2]
    program(a, b)
    KD.KTPUDevice.synchronize(SimpleNamespace(rt=dev))
    assert dev.card.collections == [1, 2, 3]
    dev.queued = False
    assert program(a, b) > 0
    assert dev.card.collections == [1, 2, 3, 4]


def test_replay_is_lazy_bounded_and_owns_completion_pins():
    dev = device(depth=2)
    a, b = Storage(dev, 32), Storage(dev, 32)
    dev.record()
    launch(dev, a, b)
    launch(dev, a, b)
    log = dev.recorded()
    assert dev.card.collections == []
    dev.drain()
    a.free()
    replayed = dev.replay(log, wait=False)
    assert dev.card.collections == [1, 2]
    log.close()
    assert dev.mmu.entries[a.addr].live
    assert len(replayed) == 2
    dev.drain()
    assert not dev.mmu.entries[a.addr].live


@pytest.mark.parametrize(
    "check,method",
    [
        (test_lazy_order_and_unrelated_host_access, "wait_address"),
        (test_host_write_waits_for_reader_and_alias, "wait_address"),
        (test_queue_depth_comes_from_layout, "_admit"),
        (test_byte_budget_counts_union_and_retires_oldest, "_admit"),
        (test_free_after_last_completion, "_retain"),
    ],
)
def test_mutants_are_killed(monkeypatch, check, method):
    monkeypatch.setattr(Device, method, lambda *args, **kwargs: [])
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        check()


def test_fault_attribution_mutant_is_killed(monkeypatch):
    def wrong(self, pending):
        return self.card.collect(pending.pending)

    monkeypatch.setattr(Device, "collect", wrong)
    with pytest.raises(AssertionError):
        test_fault_names_plan_run_completion_and_discards_later()


def test_later_collect_discovers_earlier_fault():
    dev = device()
    a, b = Storage(dev, 32), Storage(dev, 32)
    first = launch(dev, a, b, "first")
    later = launch(dev, a, b, "later")
    dev.card.fail = 1
    with pytest.raises(NodeError, match="kernel first"):
        dev.collect(later)
    assert first.error is not None and later.run is None
    assert dev.card.collections == [1]


def test_out_of_order_mutant_is_killed(monkeypatch):
    monkeypatch.setattr(Device, "collect", Device._collect_one)
    with pytest.raises(pytest.fail.Exception):
        test_later_collect_discovers_earlier_fault()


def test_diagnostic_error_keeps_original_completion():
    card = object.__new__(NodeCard)
    error = NodeError(Completion(7, 0x20, 0, 3, 19, 0))

    def fail(*args, **kwargs):
        raise error

    def diagnostic(*args):
        raise OSError("unwritable diagnostic file")

    card.nodes, card.q = (0,), SimpleNamespace(wait=fail)
    card._timeout = 1
    card._segment = lambda *args: None
    card._keep_failed = diagnostic
    pending = Pending(True, {0: (7, None)}, {0: (None, [])})
    with pytest.raises(NodeError) as caught:
        card.collect(pending)
    assert caught.value is error
    assert "unwritable" in error.__notes__[0]


def test_diagnostic_mask_mutant_is_killed(monkeypatch):
    original = NodeCard.collect

    def masked(self, pending):
        try:
            return original(self, pending)
        except NodeError as error:
            if getattr(error, "__notes__", []):
                raise OSError("diagnostic masked completion") from error
            raise

    monkeypatch.setattr(NodeCard, "collect", masked)
    with pytest.raises(OSError):
        test_diagnostic_error_keeps_original_completion()


def test_readonly_forms_refresh_and_output_alias_wins():
    dev = device()
    a, b = Storage(dev, 32), Storage(dev, 32)
    readonly = []
    with dev.submission_scope("forms", lambda: readonly):
        first = dev.run(kernel(), {"out": a.addr})
        readonly.append(a.addr)
        second = dev.run(kernel(), {"in": a.addr, "out": b.addr})
    dev.read(a.addr, 32)
    assert first.run is not None and second.run is None
    with dev.submission_scope("alias", [a.addr], [a.addr]):
        third = dev.run(kernel(), {"out": a.addr})
    dev.read(a.addr, 32)
    assert third.run is not None


def test_readonly_mutant_is_killed(monkeypatch):
    monkeypatch.setattr(Device, "_write_refs", lambda self, refs: list(refs))
    with pytest.raises(AssertionError):
        test_readonly_forms_refresh_and_output_alias_wins()


def test_replay_wait_mutant_is_killed(monkeypatch):
    original = Device._replay_queued

    def waits(self, log):
        result = original(self, log)
        self.drain()
        return result

    monkeypatch.setattr(Device, "_replay_queued", waits)
    with pytest.raises(AssertionError):
        test_replay_is_lazy_bounded_and_owns_completion_pins()
