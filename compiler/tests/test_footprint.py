"""What a call will cost, answered before it runs.

A mesh is 4 GB and nothing could check a network against it ahead of time: a
model that does not fit failed at dispatch, with an `OutOfMemory` naming one
allocation and nothing about the shape that caused it.

THE CLAIM IS EXACTNESS, not an estimate. `Kernel.footprint` prices every buffer
the call places -- operands, results, temps, tables and the backend's folded
constants -- at the allocator's own alignment, and the first test asserts it
against the bytes the arena really hands out.

The kernels are FIXTURES (rule 2b): the corpus is chosen to span no temps, a
few, folded constants and a table, which is what a footprint has to price.
"""

import numpy as np
import pytest
from kohakuaccel.lifetime import Footprint
from kohakuaccel.machinespec import MachineSpec, MeshSpec
from kohakuaccel.memory import Arena, OutOfMemory
from kohakutpu.model import SimDevice

from tests.fixtures import CORPUS, chained, masked, mm, operands, rownorm, scale


@pytest.mark.parametrize(
    ("name", "kern", "shapes", "knobs"), CORPUS, ids=[r[0] for r in CORPUS]
)
def test_the_footprint_is_the_bytes_the_arena_really_hands_out(
    name, kern, shapes, knobs
):
    """The whole point: predicted peak == measured peak, to the byte.

    An estimate would be worthless here. The question a footprint answers is
    "does this network fit in 4 GB", and an answer 10% out is the same as no
    answer at a factor the batch size can move.
    """
    dev = SimDevice(size=1 << 26)
    args = operands(dev, shapes)
    want = kern.footprint(*args, **knobs)
    before = dev.arena.peak
    kern(*args, **knobs)
    assert dev.arena.peak - before == want.peak


def test_every_buffer_the_call_places_is_priced():
    """A missing buffer is worse than a wrong number: it under-reports silently.

    Layouts cover the ports and the temps; the folded constants are the ones a
    caller never named and are the easy ones to forget.
    """
    dev = SimDevice(size=1 << 26)
    got = rownorm.footprint(*operands(dev, [(64, 64)]))
    compiled = rownorm.plan(*operands(dev, [(64, 64)]))
    assert compiled.constants(), "no folded constant here, so this proves nothing"
    assert set(got.sizes) == set(compiled.layouts) | set(compiled.constants())
    assert set(got.roles) == set(got.sizes)
    assert set(got.by_role()) == {"in", "out", "temp", "const"}


def test_a_call_that_cannot_fit_says_so_before_it_runs():
    """`fits` against the free bytes, which is the question nobody could ask."""
    dev = SimDevice(size=1 << 26)
    got = mm.footprint(*operands(dev, [(512, 512), (512, 512)]))
    assert got.fits(dev.arena.free)
    assert not got.fits(got.peak - 1)


def test_the_footprint_is_rounded_to_the_allocators_granularity():
    """A span is rounded up on allocation, so a footprint that is not under-reports."""
    dev = SimDevice(size=1 << 26)
    got = scale.footprint(*operands(dev, [(3,)]))
    assert dev.arena.align > 1
    assert all(v % dev.arena.align == 0 for v in got.sizes.values())
    assert got.peak >= dev.arena.align * len(got.sizes)


def test_a_temps_lifetime_is_the_stages_that_touch_it():
    """Reuse is only as sound as this, so it is asserted rather than inferred.

    A temp is EITHER an interval inside the stages or the whole-run default,
    and never anything else. A band that writes and reads a temp inside ONE
    stage gets default-refused to the whole run -- losing the saving, never the
    answer -- which is why `rownorm`'s first temp is whole-run and its later
    two are not.
    """
    dev = SimDevice(size=1 << 26)
    got = rownorm.footprint(*operands(dev, [(64, 64)]))
    stages = len(rownorm.plan(*operands(dev, [(64, 64)])).stages)
    established = 0
    for name, role in got.roles.items():
        first, last = got.life[name]
        if role != "temp":
            assert (first, last) == (-1, stages)
        elif (first, last) != (-1, stages):
            assert 0 <= first <= last < stages, f"{name} lives {first}..{last}"
            established += 1
    assert established, "every temp default-refused, so this proves nothing"


def test_reuse_moves_TEMPS_and_nothing_else():
    """What reuse is allowed to touch, which holds whether or not it saves.

    None of this corpus has two temps whose lifetimes are disjoint -- the
    compiler fuses elementwise passes until the survivors all overlap -- so the
    saving itself is asserted against constructed lifetimes in
    `test_lifetime.py`, where it can be made to happen on purpose.
    """
    dev = SimDevice(size=1 << 26)
    args = [(64, 64)]
    plain = rownorm.plan(*operands(dev, args)).footprint(align=256)
    shared = rownorm.plan(*operands(dev, args)).footprint(align=256, reuse=True)
    temps = {n for n, r in shared.roles.items() if r == "temp"}
    assert temps, "no temp at all here, so this proves nothing"
    assert set(shared.offsets) <= temps, "reuse moved something that is not a temp"
    assert shared.peak == plain.peak - shared.saved
    assert shared.held == plain.peak - sum(plain.sizes[n] for n in temps)

    two = operands(dev, [(64, 128), (64, 128)])
    assert mm.plan(*two).footprint(align=256, reuse=True).saved == 0


def test_a_footprint_reports_itself():
    """The diagnosis half: a peak with no breakdown does not say what to change."""
    dev = SimDevice(size=1 << 26)
    got = chained.footprint(*operands(dev, [(64, 64)]))
    text = got.report()
    assert all(name in text for name in got.sizes)
    assert "MiB" in text


def test_a_batched_kernel_sizes_its_table_for_one_element():
    """A TABLE IS ONE COPY EVERY BATCH ELEMENT READS, so it is not batched.

    `compile` deliberately leaves a table out of the `Batched` wrap, which makes
    it the one buffer whose bytes are its BLOCK shape and not its full one.
    Sizing it by the full shape over-reported a causal mask by the batch factor
    and put a phantom overlap in the aliasing map.
    """
    dev = SimDevice(size=1 << 26)
    args = operands(dev, [(2, 64, 64)])
    got = masked.footprint(*args, block=64)
    compiled = masked.plan(*args, block=64)
    assert compiled.tables, "this shape builds no table, so it proves nothing"
    for name in compiled.tables:
        assert got.sizes[name] == compiled.layouts[name].nbytes(compiled.block(name))

    before = dev.arena.peak
    masked(*args, block=64)
    assert dev.arena.peak - before == got.peak


# ------------------------------------------------------------------ per mesh
def test_each_mesh_gets_its_own_span_of_the_address_map():
    """Placement across meshes is the ADDRESS, not a second allocator.

    Every address a unit issues carries its mesh in bits [33:32], so one arena
    per `Device` at the same local base lands in four disjoint places and no two
    can hand out each other's bytes.
    """
    machine = MachineSpec(
        name="four",
        meshes=tuple(MeshSpec(i, {"MG": ((1, 1),)}) for i in range(4)),
    )
    arenas = [
        Arena(machine.global_addr(0x1800_0000, m), 1 << 26, mesh=m) for m in range(4)
    ]
    for a in arenas:
        assert MachineSpec.addr_mesh(a.base) == a.mesh
        assert MachineSpec.addr_mesh(a.base + a.size - 1) == a.mesh
    for i, a in enumerate(arenas):
        for b in arenas[i + 1 :]:
            assert a.base >= b.base + b.size or b.base >= a.base + a.size


def test_a_device_arena_carries_the_mesh_its_addresses_name():
    dev = SimDevice(size=1 << 20)
    assert dev.arena.mesh == dev.machine.default


def test_a_full_arena_says_which_mesh_is_full():
    """On a group of four, "out of memory" without a mesh is half a diagnosis."""
    arena = Arena(0x1000, 8192, mesh=2)
    with pytest.raises(OutOfMemory, match="mesh_2"):
        arena.alloc(1 << 20)
    with pytest.raises(OutOfMemory, match="does not fit"):
        Arena(0x1000, 8192).alloc(1 << 20)


def test_the_footprint_says_which_mesh_it_priced():
    dev = SimDevice(size=1 << 26)
    got = scale.footprint(*operands(dev, [(64, 64)]))
    assert got.mesh == dev.machine.default
    assert f"mesh_{got.mesh}" in got.report()


def test_a_call_too_big_for_any_arena_is_refused_with_a_breakdown():
    """The 4 GB question, asked at the call instead of at some allocation.

    A bare `OutOfMemory` names one buffer and the shape that caused it is not in
    the message; this names the peak, the arena, and every buffer.
    """
    dev = SimDevice(size=1 << 14)
    args = operands(dev, [(128, 128), (128, 128)])
    with pytest.raises(OutOfMemory, match="no allocation order fits it") as raised:
        mm(*args)
    assert "KiB" in str(raised.value)
    assert dev.arena.used == 0, "it allocated before deciding it could not"


def test_a_call_that_would_fit_an_empty_arena_is_not_refused_up_front():
    """NO FALSE REFUSALS. What is resident now is somebody's to release.

    Checking the peak against FREE bytes would refuse a call that one `del`
    makes room for, which is worse than the message it saves. So a call that
    fits the arena but not the moment fails at the allocation, as it always did.
    """
    dev = SimDevice(size=1 << 18)
    x = dev.tensor(np.zeros((64, 64), np.float16))
    want = rownorm.footprint(x)
    assert want.fits(dev.arena.size)

    hog = dev.alloc(dev.arena.free - want.peak + dev.arena.align)
    assert not want.fits(dev.arena.free)
    with pytest.raises(OutOfMemory, match="does not fit") as raised:
        rownorm(x)
    assert "no allocation order" not in str(raised.value)
    dev.free(hog)
    assert np.asarray(rownorm(x).numpy()).shape == (64, 64)


def test_an_empty_footprint_costs_nothing():
    """The degenerate case, because `peak` sums and `report` takes a max."""
    got = Footprint("nothing", 256)
    assert got.peak == 0
    assert got.saved == 0
    assert got.by_role() == {}
    assert "nothing" in got.report()
