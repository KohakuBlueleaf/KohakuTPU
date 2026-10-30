"""Graph placement retains extents, never old owners or outstanding accesses."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime.arena import Arena, OutOfMemory
from kohakutpu.application.runtime.device import Device
from kohakutpu.application.runtime.placement import Placement
from kohakutpu.application.runtime.storage import Storage
from kohakutpu.stack.kernel import arena_name


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


def test_reuse_waits_for_the_last_access(dev):
    pool = Placement(dev)
    collected = []
    dev.card.submit = lambda program: object()
    dev.card.collect = lambda pending: collected.append(pending) or {"cycles": 0}
    compiled = SimpleNamespace(
        name="reader",
        l1=SimpleNamespace(params=lambda *args: []),
        scratch=[],
        programs=lambda binds: {None: object()},
    )
    with dev.placement_scope(pool, 0):
        old = dev.alloc(1024)
    first = dev.submit(compiled, {"x": old})
    physical = dev.mmu.resolve(old)[1]
    dev.free(old)
    assert not collected
    with dev.placement_scope(pool, 0):
        new = dev.alloc(1024)
    assert collected == [first.pending]
    assert dev.mmu.resolve(new)[1] == physical
    with pytest.raises(ValueError, match="live owner"):
        dev.mmu.resolve(old)
    pool.close()
    assert physical in dev.dram.live
    dev.free(new)
    assert dev.dram.used == 0


def per_site(dev):
    """`Device._windowed` as a span a run: each through `alloc` (a graph
    site's own pinned span), never freed."""
    return lambda spans: {
        name: dev.alloc(nbytes, space, node, owner="scratch")
        for name, nbytes, space, node, _ in spans
    }


def arena_of(dev, space):
    return dev.memory.staging[0] if space == "stage" else dev.dram


def key_of(space):
    return (space, 0 if space == "stage" else None)


@pytest.mark.parametrize("space", ["stage", "dram"])
@pytest.mark.parametrize("window", [True, False])
def test_a_graph_pins_one_scratch_window_a_space(dev, monkeypatch, space, window):
    """Eight sites of scratch, a quarter of the space's arena each, recorded
    as one graph call: every site's run binds the space's one window. With a
    span a site instead (no window), the fifth does not fit."""
    arena = arena_of(dev, space)
    room = arena.hi - arena.lo
    dev.card.submit = lambda program: object()
    dev.card.collect = lambda pending: {"cycles": 0}
    bound = []
    scratch = {
        "bytes": room // 4,
        "space": space,
        "node": 0 if space == "stage" else None,
    }
    name = arena_name(scratch)
    compiled = SimpleNamespace(
        name="ring",
        l1=SimpleNamespace(params=lambda *args: []),
        scratch=[scratch],
        programs=lambda binds: bound.append(binds[name]) or {None: object()},
    )
    if not window:
        monkeypatch.setattr(dev, "_windowed", per_site(dev))
    pool = Placement(dev)
    dev.record()
    try:
        for site in range(8):
            with dev.placement_scope(pool, site):
                dev.submit(compiled, {})
    except OutOfMemory:
        assert not window and len(bound) == 4
        return
    finally:
        log = dev.recorded()
        dev.drain()
    assert window and len(bound) == 8 and set(bound) == {dev.windows[key_of(space)][0]}
    assert arena.used == room // 4
    log.close()
    pool.close()
    assert arena.used == room // 4


@pytest.mark.parametrize("space", ["stage", "dram"])
def test_a_grown_window_stays_pinned_by_the_graph_that_recorded_it(dev, space):
    """A run that outgrows the window takes a larger one; the smaller stays
    live while a recording holds it and is freed when it closes. With no run
    in flight the window a recording holds is kept, the one none holds
    freed."""
    dev.card.submit = lambda program: object()
    dev.card.collect = lambda pending: {"cycles": 0}

    def compiled(nbytes):
        return SimpleNamespace(
            name="ring",
            l1=SimpleNamespace(params=lambda *args: []),
            scratch=[{"bytes": nbytes, "space": space, "node": 0}],
            programs=lambda binds: {None: object()},
        )

    arena = arena_of(dev, space)
    dev.record()
    dev.submit(compiled(4096), {})
    log = dev.recorded()
    dev.drain()
    small = dev.mmu.resolve(dev.windows[key_of(space)][0])[1]
    dev.submit(compiled(8192), {})
    large = dev.mmu.resolve(dev.windows[key_of(space)][0])[1]
    assert large != small and small in arena.live and small in arena.released
    dev.drain()
    assert key_of(space) not in dev.windows and large not in arena.live
    assert small in arena.live
    log.close()
    assert small not in arena.live and arena.used == 0


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
