"""The arenas and the software MMU: native against Python, homes against the DRAM map."""

import random

import pytest

from kohakuaccel.driver.runtime import mmu as driver_mmu
from kohakuaccel.driver.runtime.mmu import SoftwareMMU
from kohakuaccel.driver.transport.simdram import DramMap
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.application.runtime.memory.arena import Arena, OutOfMemory, place
from kohakutpu.stack.rs import _rs

BOARD = {"dram_interleave_kb": 16, "dram_flat_gb": 16, "dram_homes": 4}
MAP = DramMap.of_board(BOARD)
STRIPE, HOMES = 16 << 10, 4
SIZES = (256, STRIPE - 256, STRIPE, STRIPE + 256, 3 * STRIPE)
STARTS = (0, 256, STRIPE - 256, STRIPE, 3 * STRIPE + 512)


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


def test_native_memory_is_the_python_memory():
    assert exercise(_rs.Arena(0x1000, 0x5000), _rs.NativeMMU()) == exercise(
        Arena(0x1000, 0x5000), SoftwareMMU()
    )


def test_every_arena_refuses_with_out_of_memory():
    assert OutOfMemory is not MemoryError and issubclass(OutOfMemory, MemoryError)
    for arena in (_rs.Arena(0x1000, 0x2000), Arena(0x1000, 0x2000)):
        with pytest.raises(OutOfMemory):
            arena.alloc(0x2000)


def test_a_carve_keeps_the_arena_class_of_its_memory(monkeypatch):
    for native, expected in (("0", Arena), ("1", _rs.Arena)):
        monkeypatch.setenv("KTPU_NATIVE_MEMORY", native)
        memory = CardMemory((0x1000, 0x9000), {0: (0x10000, 0x14000)})
        monkeypatch.setenv("KTPU_NATIVE_MEMORY", "0" if native == "1" else "1")
        assert isinstance(memory.dram, expected)
        assert isinstance(memory.staging[0], expected)
        assert isinstance(memory.carve(0x1000), expected)


def test_the_native_mmu_takes_its_domain_from_the_driver(monkeypatch):
    monkeypatch.setattr(driver_mmu, "VIRTUAL_OFFSET", 2 << 32)
    mmu = _rs.NativeMMU()
    arena = _rs.Arena(0x1000, 0x5000)
    assert mmu.virtual_offset == 2 << 32
    assert mmu.register(arena.alloc(256, "test"), 256, arena) == 2 << 32


@pytest.mark.parametrize("home", range(HOMES))
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("lo", STARTS)
def test_a_package_stays_on_its_home(lo, size, home):
    spans = [(lo, 1 << 24)]
    at = place(spans, size, (STRIPE, HOMES, home))
    _, got, _, run = next(MAP.runs(at, size))
    assert (got, run) == (home, min(size, STRIPE))
    assert at >= lo
    assert sum(n for _, n in spans) == (1 << 24) - size


def test_without_a_home_is_first_fit():
    spans = [(0, 256), (4096, 4096)]
    assert place(spans, 512) == 4096
    assert spans == [(0, 256), (4608, 3584)]


def test_a_full_home_falls_back_to_first_fit():
    spans = [(STRIPE, 4096)]
    assert place(spans, 256, (STRIPE, HOMES, 0)) == STRIPE
    assert spans == [(STRIPE + 256, 4096 - 256)]
    assert place([(0, 256)], 512, (STRIPE, HOMES, 0)) is None


def test_native_and_python_arenas_place_alike():
    rng = random.Random(7)
    arenas = _rs.Arena(0x1000, 1 << 26), Arena(0x1000, 1 << 26)
    live = []
    for _ in range(2000):
        if live and rng.random() < 0.45:
            at = live.pop(rng.randrange(len(live)))
            for arena in arenas:
                arena.release(at)
        else:
            size = rng.choice((1, 256, 300, 4096, STRIPE - 64, STRIPE, STRIPE * 2 + 1))
            home = rng.randrange(HOMES + 1)
            near = None if home == HOMES else (STRIPE, HOMES, home)
            at = [arena.alloc(size, "package", near) for arena in arenas]
            assert at[0] == at[1]
            live.append(at[0])
        assert arenas[0].free_list == arenas[1].free_list
