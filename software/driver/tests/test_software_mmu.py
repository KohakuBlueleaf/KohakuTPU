"""Virtual allocation identity survives physical reuse without stale aliases."""

import pytest

from kohakuaccel.driver.runtime import mmu as MMU
from kohakuaccel.driver.runtime.mmu import SoftwareMMU
from kohakutpu.application.runtime.arena import Arena
from kohakutpu.application.runtime.device import Device


def test_non_reused_handles_and_stale_generation_refusal():
    arena = Arena(0x400000, 0x500000)
    mmu = SoftwareMMU()
    physical = arena.alloc(256)
    first = mmu.register(physical, 256, arena)
    assert first != physical
    assert mmu.resolve(first + 32, 224)[1] == physical + 32
    arena.release(physical)
    assert arena.alloc(256) == physical
    second = mmu.register(physical, 256, arena)
    assert second != first
    with pytest.raises(ValueError, match="RUL.023"):
        mmu.resolve(first)
    assert mmu.translations() == ((second, physical, 256),)
    with pytest.raises(ValueError, match="extent"):
        mmu.resolve(second + 224, 33)


def test_virtual_host_io_does_not_touch_virtual_physical_location():
    dev = Device(kind="offline", board="multimesh_v9t6")
    a = dev.alloc(32)
    _, physical = dev.mmu.resolve(a)
    dev.write(a, b"a" * 32)
    assert dev.card.get(physical, 32) == b"a" * 32
    assert dev.card.get(a, 32) == bytes(32)
    dev.free(a)
    b = dev.alloc(32)
    assert dev.mmu.resolve(b)[1] == physical
    assert b != a
    with pytest.raises(ValueError, match="RUL.023"):
        dev.read(a, 32)
    dev.free(b)
    dev.close()


def test_staging_handle_keeps_node_domain():
    mmu = SoftwareMMU()
    for node in (0, 1):
        base = (1 << 39) | (node << 36)
        arena = Arena(base + 0x100000, base + 0x200000)
        physical = arena.alloc(256)
        virtual = mmu.register(physical, 256, arena)
        assert virtual >> 36 == physical >> 36
        assert mmu.resolve(virtual)[1] == physical


def test_retired_entries_are_bounded_and_evicted_handles_still_refuse(monkeypatch):
    monkeypatch.setattr(MMU, "RETIRED_KEPT", 8)
    arena = Arena(0x400000, 0x500000)
    mmu = SoftwareMMU()
    mmu._prune_at = 4
    pinned = mmu.register(arena.alloc(256), 256, arena)
    retired = []
    for _ in range(40):
        pa = arena.alloc(256)
        va = mmu.register(pa, 256, arena)
        retired.append(va)
        arena.release(pa)
        mmu.retire(va)
    assert len(mmu._live) == 1
    assert len(mmu.entries) <= 1 + 8
    assert mmu.translations() == ((pinned, 0x400000, 256),)
    # Evicted ancient handles and retained recent retired handles both refuse with RUL.023.
    for old in (retired[0], retired[-1]):
        with pytest.raises(ValueError, match="RUL.023"):
            mmu.resolve(old)
    # Witness: without retirement, `_live` and `entries` grow with total allocations.
    unbounded = SoftwareMMU()
    monkeypatch.setattr(unbounded, "_retire", lambda at: None)
    for _ in range(40):
        pa = arena.alloc(256)
        va = unbounded.register(pa, 256, arena)
        arena.release(pa)
        unbounded.retire(va)
    assert len(unbounded._live) > 8
    assert len(unbounded.entries) > 1 + 8
