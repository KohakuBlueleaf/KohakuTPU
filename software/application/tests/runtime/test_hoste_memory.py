"""Native arena/MMU parity includes released pins and reused physical spans."""

import pytest

from kohakuaccel.driver.runtime import mmu as driver_mmu
from kohakuaccel.driver.runtime.mmu import SoftwareMMU
from kohakutpu.application.runtime.arena import Arena, OutOfMemory
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs


def exercise(arena, mmu):
    a = arena.alloc(256, "input")
    b = arena.alloc(512, "output")
    h = mmu.register(a, 256, arena)
    other = mmu.register(b, 512, arena)
    arena.retain(a)
    arena.release(a)
    mmu.retire(h)
    assert mmu.resolve(h + 17, 3)[1] == a + 17
    with pytest.raises(ValueError, match="live owner"):
        mmu.resolve(h, owner=True)
    arena.unretain(a)
    mmu.retire(h)
    reused = arena.alloc(256, "next")
    assert reused == a
    new = mmu.register(reused, 256, arena)
    assert new != h
    with pytest.raises(ValueError, match="live owner"):
        mmu.resolve(h)
    with pytest.raises(ValueError, match="extent"):
        mmu.resolve(other + 510, 3)
    translation = mmu.translations_for([new, other + 64])
    arena.release(b)
    arena.release(reused)
    assert arena.free_list == [(0x1000, 0x4000)]
    top = arena.alloc_top(300, "top")
    return new, sorted(translation), top, arena.stats()


def test_mutant_native_memory_reuses_retired_virtual_handle_is_caught():
    assert "bindings.mmu/reuse_handle" in rs.mutants()
    exercise(_rs.Arena(0x1000, 0x5000), _rs.NativeMMU())
    with (
        rs.mutant("bindings.mmu/reuse_handle"),
        pytest.raises(AssertionError),
    ):
        exercise(_rs.Arena(0x1000, 0x5000), _rs.NativeMMU())


def test_native_memory_generations_pin_lifetime_and_allocator_parity():
    assert OutOfMemory is not MemoryError and issubclass(OutOfMemory, MemoryError)
    for arena in (_rs.Arena(0x1000, 0x2000), Arena(0x1000, 0x2000)):
        with pytest.raises(OutOfMemory):
            arena.alloc(0x2000)
    assert exercise(_rs.Arena(0x1000, 0x5000), _rs.NativeMMU()) == exercise(
        Arena(0x1000, 0x5000), SoftwareMMU()
    )


def test_card_memory_latches_arena_class_and_mmu_shares_domain_constants(monkeypatch):
    def latch_witness():
        for native, expected in (("0", Arena), ("1", _rs.Arena)):
            monkeypatch.setenv("KTPU_NATIVE_MEMORY", native)
            mem = CardMemory((0x1000, 0x9000), {0: (0x10000, 0x14000)})
            monkeypatch.setenv("KTPU_NATIVE_MEMORY", "0" if native == "1" else "1")
            carved = mem.carve(0x1000)
            assert isinstance(mem.dram, expected)
            assert isinstance(mem.staging[0], expected)
            assert isinstance(carved, expected)

    latch_witness()
    assert "app.memory/unlatched_carve" in rs.mutants()
    with rs.mutant("app.memory/unlatched_carve"), pytest.raises(AssertionError):
        latch_witness()

    # NativeMMU reads domain constants from kohakuaccel.driver.runtime.mmu (one source).
    def domain_witness():
        monkeypatch.setattr(driver_mmu, "VIRTUAL_OFFSET", 2 << 32)
        mmu = _rs.NativeMMU()
        arena = _rs.Arena(0x1000, 0x5000)
        at = arena.alloc(256, "test")
        assert mmu.virtual_offset == driver_mmu.VIRTUAL_OFFSET
        assert mmu.register(at, 256, arena) == 2 << 32

    domain_witness()
    assert "bindings.mmu/own_offset" in rs.mutants()
    with rs.mutant("bindings.mmu/own_offset"), pytest.raises(AssertionError):
        domain_witness()
