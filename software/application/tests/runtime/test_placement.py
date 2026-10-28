"""Graph placement retains extents, never old owners or outstanding accesses."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime.arena import Arena
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.placement import Placement
from kohakutpu.application.runtime.storage import Storage


@pytest.fixture
def dev():
    device = Device(kind="offline", board="vlt_v9t6s_1n")
    yield device
    device.close()


def assert_stale(dev, handles):
    for old in handles:
        assert not dev.mmu.entries[old].live
        with pytest.raises(ValueError, match="live owner"):
            dev.mmu.resolve(old, owner=True)


@pytest.mark.parametrize("space", ["dram", "stage"])
def test_reuse_has_fresh_handles_and_releases_all_pins(dev, space):
    pool = Placement(dev)
    logs = []
    handles, physicals = [], []
    for _ in range(4):
        dev.record()
        with dev.placement_scope(pool, 0):
            at = dev.alloc(33, space, 0)
        entry, physical = dev.mmu.resolve(at)
        assert_stale(dev, handles)
        assert entry.extent == 64
        assert entry.arena.requested[physical] == 33
        assert physical % dev.line_bytes == 0
        dev.write(at, bytes(33))
        logs.append(dev.recorded())
        if len(handles) < 3:
            logs[-1].close()
        handles.append(at)
        physicals.append(physical)
        dev.free(at)
        for old in handles:
            with pytest.raises(ValueError, match="live owner"):
                dev.mmu.resolve(old, owner=True)
    assert len(set(handles)) == 4 and len(set(physicals)) == 1
    assert pool.nbytes == 256
    # A closed graph does not revoke recording or in-flight references.
    pool.close()
    assert physical in entry.arena.live
    for log in logs:
        log.close()
    pool.close()
    assert physical not in entry.arena.live


def assert_reuse_refused(arena, at):
    generation = arena.generations[at]
    with pytest.raises(ValueError, match="released, pinned"):
        arena.reuse_pinned(at)
    assert at in arena.released and arena.generations[at] == generation
    assert arena.references[at] == 2


def test_arena_reuse_requires_exclusive_pin(monkeypatch):
    arena = Arena(0, 1024)
    at = arena.alloc(64)
    arena.retain(at)
    arena.retain(at)
    arena.release(at)
    assert_reuse_refused(arena, at)
    with monkeypatch.context() as patch:
        patch.setattr(arena, "reuse_pinned", lambda at: None)
        with pytest.raises(pytest.fail.Exception):
            assert_reuse_refused(arena, at)
    arena.unretain(at)
    arena.reuse_pinned(at)
    arena.release(at)
    arena.unretain(at)
    assert arena.used == 0


def test_an_independent_recording_prevents_reuse(dev):
    pool = Placement(dev)
    dev.record()
    with dev.placement_scope(pool, 0):
        old = dev.alloc(64)
    dev.write(old, b"a" * 64)
    log = dev.recorded()
    physical = dev.mmu.resolve(old)[1]
    dev.free(old)
    with dev.placement_scope(pool, 0):
        new = dev.alloc(64)
    assert dev.mmu.resolve(new)[1] != physical
    dev.write(new, b"b" * 64)
    dev.replay(log)
    assert dev.read(new, 64) == b"b" * 64
    pool.close()
    assert physical in dev.dram.live
    log.close()
    assert physical not in dev.dram.live
    dev.free(new)
    assert dev.dram.used == 0


def test_sites_signatures_live_forms_and_graphs_are_disjoint(dev):
    first, second = Placement(dev), Placement(dev)
    with dev.placement_scope(first, 0):
        form = Storage(dev, 128)
        temp = dev.alloc(256)
    dev.free(temp)
    with dev.placement_scope(first, 0):
        # A signature's absent warm-up allocation does not shift another one.
        again = dev.alloc(256)
    assert dev.mmu.resolve(again)[1] == dev.mmu.entries[temp].physical
    with dev.placement_scope(first, 1):
        other_site = dev.alloc(256)
    with dev.placement_scope(second, 0):
        other_graph = dev.alloc(256)
    assert len({dev.mmu.resolve(a)[1] for a in (again, other_site, other_graph)}) == 3
    first.close()
    second.close()
    dev.write(form.addr, b"x" * 128)
    for at in (again, other_site, other_graph):
        dev.free(at)
    form.free()
    assert dev.dram.used == 0


def test_reuse_waits_for_the_last_access_and_scratch_owner(dev):
    pool = Placement(dev)
    collected = []
    dev.card.submit = lambda program: object()
    dev.card.collect = lambda pending: collected.append(pending) or {"cycles": 0}
    compiled = SimpleNamespace(
        name="scratch",
        l1=SimpleNamespace(params=lambda *args: []),
        scratch=[{"bytes": 1024, "space": "dram"}],
        programs=lambda binds: {None: object()},
    )
    with dev.placement_scope(pool, 0):
        first = dev.submit(compiled, {})
    old = first.scratch[0]
    physical = dev.mmu.resolve(old)[1]
    assert not collected
    with dev.placement_scope(pool, 0):
        second = dev.submit(compiled, {})
    assert collected == [first.pending]
    assert dev.mmu.resolve(second.scratch[0])[1] == physical
    with pytest.raises(ValueError, match="live owner"):
        dev.mmu.resolve(old)
    pool.close()
    assert physical in dev.dram.live
    dev.collect(second)
    assert dev.dram.used == 0


def test_generation_mutant_is_killed(dev, monkeypatch):
    pool = Placement(dev)
    with dev.placement_scope(pool, 0):
        old = dev.alloc(32)
    dev.free(old)
    monkeypatch.setattr(dev.dram, "reuse_pinned", dev.dram.released.remove)
    with dev.placement_scope(pool, 0):
        new = dev.alloc(32)
    # The mutant revives the old generation even though virtual bases differ.
    assert new != old
    with pytest.raises(AssertionError):
        assert_stale(dev, [old])
    dev.free(new)
    pool.close()


def test_device_close_before_placement_and_exception_restore(dev):
    pool = Placement(dev)
    with pytest.raises(RuntimeError, match="body"), dev.placement_scope(pool, 0):
        dev.alloc(64)
        raise RuntimeError("body")
    assert dev._placement is None
    dev.close()
    pool.close()
    assert pool.nbytes == 0
    with pytest.raises(RuntimeError, match="closed"), dev.placement_scope(pool, 0):
        dev.alloc(64)
