"""Several meshes as one group: what a split means, and what it refuses.

The two hardware-verified prototypes are the specification -- `split_matmul.py`
(column-parallel, 3.98x compute over four meshes) and the contraction split
(1.76x at one fp16 ULP), whose harness retired with the old stack. What they do
that the models cannot
is cross a link, so the collective ARITHMETIC is checked against numpy on
`SimDevice` and the interlink half is checked as the words it encodes.

The refusals are half the point. An automatic sharder has to reproduce this set,
and a spec nothing checks is worse than no spec at all.
"""

import numpy as np
import pytest
from kohakutpu.isa import ISA
from kohakutpu.meshes import (
    COPIED,
    PARTIAL,
    MeshGroup,
    Pin,
    Plan,
    Sharded,
    Spec,
    Step,
    _readout,
    _remote,
    pairing,
)
from kohakutpu.model import SimDevice

from kohakutpu import ops as O

M, K_, N = 32, 128, 64
TILING = {"gm": 8, "gn": 16, "nk": 2}


@pytest.fixture(scope="module")
def four():
    """Four devices as one group. Module-scoped: a fresh one recompiles."""
    return MeshGroup([SimDevice() for _ in range(4)])


@pytest.fixture(scope="module")
def two():
    return MeshGroup([SimDevice() for _ in range(2)])


@pytest.fixture(scope="module")
def operands():
    """`a`, `b` and the fp32 answer, at the prototypes' distributions."""
    rng = np.random.default_rng(11)
    a = rng.normal(0, 0.02, (M, K_)).astype(np.float16)
    b = rng.normal(0, 1.00, (N, K_)).astype(np.float16)
    return a, b, a.astype(np.float32) @ b.astype(np.float32).T


# --------------------------------------------------------------------- the spec
def test_a_value_cannot_be_both_split_and_partial():
    """A partial summand is whole-shaped on every rank by construction."""
    with pytest.raises(ValueError, match="both split and partial"):
        Spec(axis=0, partial=True)


def test_an_uneven_split_is_refused_rather_than_padded():
    """Padding silently would give the ranks different extents to compile against."""
    with pytest.raises(ValueError, match="not a multiple of 4"):
        Spec.on(0).local((6, 8), 4)


def test_an_axis_the_value_does_not_have_is_named():
    with pytest.raises(ValueError, match="axis 3 does not exist"):
        Spec.on(3).local((6, 8), 2)


def test_a_copy_is_the_same_shape_on_every_rank():
    assert COPIED.local((6, 8), 4) == (6, 8)
    assert Spec.on(1).local((6, 8), 4) == (6, 2)


# -------------------------------------------------------------------- the group
def test_two_devices_on_one_mesh_are_refused():
    """One transport and one arena is two Devices opened on one mesh.

    Nothing on the card reports it: the two hand out each other's spans and both
    read plausible numbers.
    """
    a, b = SimDevice(), SimDevice()
    b.transport = a.transport
    with pytest.raises(ValueError, match="same bytes"):
        MeshGroup([a, b])


def test_the_same_device_twice_is_refused():
    dev = SimDevice()
    with pytest.raises(ValueError, match="same device"):
        MeshGroup([dev, dev])


def test_an_empty_group_is_refused():
    with pytest.raises(ValueError, match="at least one device"):
        MeshGroup([])


def test_one_mesh_is_a_legal_group(operands):
    """Omitting the mesh axis means one mesh, exactly as today."""
    a, b, want = operands
    one = MeshGroup([SimDevice()])
    y = one.matmul(one.copy(a), one.copy(b), **TILING)
    assert y.spec == COPIED
    assert np.median(np.abs(y.numpy() - want)) < 1e-2


# ---------------------------------------------------------------- what splits
def test_column_parallel_agrees_with_numpy(four, operands):
    """`split_matmul.py`'s shape: `b` cut on N, `a` copied, nothing exchanged."""
    a, b, want = operands
    y = four.matmul(four.copy(a), four.split(b, 0), **TILING)
    assert y.spec == Spec.on(1)
    assert y.numpy().shape == (M, N)
    assert np.median(np.abs(y.numpy() - want)) < 1e-2


def test_column_parallel_leaves_every_piece_where_it_was(four, operands):
    """The claim that it needs no collective, as an assertion about placement."""
    a, b, _ = operands
    y = four.matmul(four.copy(a), four.split(b, 0), **TILING)
    assert [p.device for p in y.parts] == list(four.devices)
    assert y.local == (M, N // 4)


def test_row_parallel_agrees_with_numpy(four, operands):
    """`a` cut on M instead: the other split that exchanges nothing."""
    a, b, want = operands
    y = four.matmul(four.split(a, 0), four.copy(b), gm=2, gn=16, nk=2)
    assert y.spec == Spec.on(0)
    assert np.median(np.abs(y.numpy() - want)) < 1e-2


def test_a_contraction_split_is_a_partial_and_not_an_answer(two, operands):
    a, b, _ = operands
    y = two.matmul(two.split(a, 1), two.split(b, 1), **TILING)
    assert y.spec == PARTIAL
    with pytest.raises(ValueError, match="partial sum"):
        y.numpy()


def test_the_summands_add_up_to_the_whole(two, operands):
    """Added on the HOST, which is what the remote drain exists to avoid."""
    a, b, want = operands
    y = two.matmul(two.split(a, 1), two.split(b, 1), **TILING)
    got = sum(p.numpy().astype(np.float32) for p in y.parts)
    assert np.median(np.abs(got - want)) < 1e-2


def test_an_elementwise_kernel_runs_per_mesh(four):
    """`each` is the shape-preserving case: same spec in, same spec out."""
    rng = np.random.default_rng(5)
    u = rng.normal(0, 1, (M, N)).astype(np.float16)
    v = rng.normal(0, 1, (M, N)).astype(np.float16)
    got = four.each(O.residual, four.split(u, 0), four.split(v, 0))
    assert got.spec == Spec.on(0)
    assert np.abs(got.numpy() - (u + v)).max() < 1e-2


# ----------------------------------------------------------------- the refusals
REFUSED = [
    ("both output axes", Spec.on(0), Spec.on(0)),
    ("only a splits K", Spec.on(1), COPIED),
    ("only b splits K", COPIED, Spec.on(1)),
    ("rows against K", Spec.on(0), Spec.on(1)),
    ("crossed", Spec.on(1), Spec.on(0)),
]


@pytest.mark.parametrize(("why", "a", "b"), REFUSED, ids=[r[0] for r in REFUSED])
def test_a_pairing_with_no_collective_free_reading_is_refused(why, a, b):
    """Re-sharding silently would re-upload a weight over an unbudgeted link."""
    with pytest.raises(ValueError, match="not a split this layer will run"):
        pairing(a, b)


def test_the_three_legal_pairings():
    assert pairing(COPIED, Spec.on(0)) == Spec.on(1)
    assert pairing(Spec.on(0), COPIED) == Spec.on(0)
    assert pairing(Spec.on(1), Spec.on(1)) == PARTIAL
    assert pairing(COPIED, COPIED) == COPIED


def test_a_partial_cannot_be_fed_to_another_matmul(two, operands):
    a, b, _ = operands
    y = two.matmul(two.split(a, 1), two.split(b, 1), **TILING)
    with pytest.raises(ValueError, match="no partial sum"):
        pairing(y.spec, COPIED)


def test_a_plain_tensor_names_the_fix(four, operands):
    """It lives on ONE mesh, and the other ranks cannot read that memory."""
    a, b, _ = operands
    with pytest.raises(TypeError, match=r"group\.copy"):
        four.matmul(four[0].tensor(a), four.split(b, 0), **TILING)


def test_an_operand_from_another_group_is_refused(four, operands):
    a, b, _ = operands
    other = MeshGroup([SimDevice() for _ in range(4)])
    with pytest.raises(ValueError, match="another group"):
        four.matmul(other.copy(a), four.split(b, 0), **TILING)


def test_operands_cut_two_different_ways_are_refused(four):
    """`each` moves nothing between meshes, so it cannot reconcile them."""
    u = np.ones((M, N), np.float16)
    with pytest.raises(ValueError, match="cut the same way"):
        four.each(O.residual, four.split(u, 0), four.copy(u))


def test_contracting_mismatched_axes_names_both(four, operands):
    a, b, _ = operands
    with pytest.raises(ValueError, match="last axes are 128 and 64"):
        four.matmul(four.copy(a), four.copy(b[:, :64]), **TILING)


# ------------------------------------------------- placement against sequencing
def test_a_plan_places_and_runs_nothing(four, operands):
    """The seam a device-side orchestrator replaces: `run`, and nothing above it."""
    a, b, _ = operands
    before = [s["dispatches"] for s in four.stats()]
    plan = four.plan_matmul(four.copy(a), four.split(b, 0), **TILING)
    assert [s["dispatches"] for s in four.stats()] == before
    assert [s.rank for s in plan.steps] == [0, 1, 2, 3]
    four.run(plan)
    assert all(
        n > was for n, was in zip([s["dispatches"] for s in four.stats()], before)
    )


def test_a_reduce_plan_opens_the_destination_tile_first(two, operands):
    """`dbuf=2` ADDS, and tile memory has no reset (isa/cluster.md §9.4)."""
    a, b, _ = operands
    plan = two.plan_reduce(two.split(a, 1), two.split(b, 1), into=1, **TILING)
    assert plan.into == 1
    assert plan.steps[0].rank == 1
    assert plan.steps[0].node == plan.tile
    assert [s.rank for s in plan.steps] == [1, 0]
    assert all(s.node is not None for s in plan.steps)


def test_a_reduce_plan_run_out_of_order_is_refused(two, operands):
    """The 65504 failure, as the one assertion that can catch it from the host."""
    a, b, _ = operands
    plan = two.plan_reduce(two.split(a, 1), two.split(b, 1), into=1, **TILING)
    backwards = Plan(
        tuple(reversed(plan.steps)), plan.spec, plan.shape, plan.into, plan.tile
    )
    with pytest.raises(ValueError, match="65504"):
        two.run(backwards)


def test_a_reduce_needs_a_kernel_that_fits_one_cluster(two, operands):
    """The accumulator being added into belongs to ONE cluster."""
    a, b, _ = operands
    with pytest.raises(ValueError, match="ONE cluster"):
        two.plan_reduce(two.split(a, 1), two.split(b, 1), into=0, gm=8, gn=4, nk=2)


def test_a_reduce_deeper_than_the_accumulator_is_refused(two):
    """`gm*gn` past the depth compiles clean, models clean, and wraps on the card.

    A grid of (1, 1) does not mean it fits: `_one_instance` bounds the grid and
    the accumulator bounds `gm*gn`, and 32x32 is one instance of 1024 sub-tiles
    against ship_3x2's 512.
    """
    rng = np.random.default_rng(31)
    a = rng.normal(0, 0.02, (128, K_)).astype(np.float16)
    b = rng.normal(0, 1.00, (128, K_)).astype(np.float16)
    with pytest.raises(ValueError, match="1024 sub-tiles"):
        two.plan_reduce(two.split(a, 1), two.split(b, 1), into=0, gm=32, gn=32, nk=2)


def test_the_deepest_tiling_the_accumulator_holds_is_allowed(two):
    """512 sub-tiles is 64x128, four times the shape the 1.76x was measured at."""
    rng = np.random.default_rng(32)
    a = rng.normal(0, 0.02, (64, K_)).astype(np.float16)
    b = rng.normal(0, 1.00, (128, K_)).astype(np.float16)
    plan = two.plan_reduce(two.split(a, 1), two.split(b, 1), into=0, gm=16, gn=32, nk=2)
    assert plan.shape == (64, 128)
    assert plan.steps[0].rank == 0


def test_the_readout_re_drains_a_whole_accumulator(operands):
    """The cap is the accumulator's 512 sub-tiles, not the 128 that were measured."""
    rng = np.random.default_rng(33)
    a = rng.normal(0, 0.02, (64, K_)).astype(np.float16)
    b = rng.normal(0, 1.00, (128, K_)).astype(np.float16)
    dev = SimDevice()
    tile = dev.machine.coords("MG")[2]
    with Pin(dev, tile):
        y = O.matmul(dev.tensor(a), dev.tensor(b), gm=16, gn=32, nk=2)
    once = y.numpy()
    y.host = None
    assert np.array_equal(_readout(dev, y, tile, (64, 128)).numpy(), once)


def test_a_reduce_over_one_mesh_crosses_no_link(operands):
    a, b, _ = operands
    one = MeshGroup([SimDevice()])
    with pytest.raises(ValueError, match="crosses no link"):
        one.plan_reduce(one.split(a, 1), one.split(b, 1), into=0, **TILING)


def test_a_split_that_needs_no_reduce_refuses_one(two, operands):
    a, b, _ = operands
    with pytest.raises(ValueError, match="need no reduce"):
        two.plan_reduce(two.copy(a), two.split(b, 0), into=0, **TILING)


def test_a_cross_mesh_drain_without_a_card_is_refused(two, operands):
    """The models decode a node-addressed DRAIN and have no interlink at all."""
    a, b, _ = operands
    with pytest.raises(ValueError, match="has no card"):
        two.matmul_reduce(two.split(a, 1), two.split(b, 1), into=1, **TILING)


# --------------------------------------------------------------- the mechanism
def test_a_pin_sends_every_instance_to_one_unit(operands):
    """Two meshes cannot agree which accumulator to add into without this."""
    a, b, want = operands
    dev = SimDevice()
    tile = dev.machine.coords("MG")[3]
    with Pin(dev, tile):
        y = matmul_on(dev, a, b)
    busy = {
        c: u.counts["GEMM"] for c, u in sorted(dev.card.units.items()) if _cluster(u)
    }
    assert busy[tile] > 0
    assert sum(v for c, v in busy.items() if c != tile) == 0
    assert np.median(np.abs(y.numpy() - want)) < 1e-2


def test_a_pin_is_removed_afterwards():
    dev = SimDevice()
    with Pin(dev, dev.machine.coords("MG")[0]):
        assert "dispatch" in dev.__dict__
    assert "dispatch" not in dev.__dict__


def test_the_readout_re_drains_the_same_accumulator(operands):
    """A drain does not clear the tile, which is what lets a partial be added in."""
    a, b, _ = operands
    dev = SimDevice()
    tile = dev.machine.coords("MG")[3]
    with Pin(dev, tile):
        y = matmul_on(dev, a, b)
    once = y.numpy()
    y.host = None
    assert np.array_equal(_readout(dev, y, tile, (M, N)).numpy(), once)


def test_the_readout_refuses_a_buffer_the_layout_padded(operands):
    a, b, _ = operands
    dev = SimDevice()
    tile = dev.machine.coords("MG")[3]
    with Pin(dev, tile):
        y = matmul_on(dev, a, b)
    with pytest.raises(ValueError, match="misaligned"):
        _readout(dev, y, tile, (M, N + 8))


def test_a_remote_drain_carries_the_mesh_and_the_far_cluster(operands):
    """Patched onto a DRAIN the compiler emitted, not one written for the test."""
    a, b, _ = operands
    dev = SimDevice()
    seen = []

    def spy(word):
        seen.append(_remote(word, (0, 1), 2, (2, 3)))
        return word

    with Pin(dev, dev.machine.coords("MG")[0], spy):
        matmul_on(dev, a, b)
    name, f = ISA.set.decode(seen[0])
    assert name == "DRAIN"
    assert (f["dnode"], f["dbuf"], f["dflags"]) == (1, 2, 1)
    assert (f["dmesh"], f["dfin"]) == (2, 0x32)
    assert (f["dst_x"], f["dst_y"]) == (0, 1)
    assert (f["dack_x"], f["dack_y"]) == (0, 1)
    assert f["addr"] == 0, "buf 2 is a sub-tile pair, so the granule must be even"
    assert f["n"] == TILING["gm"] * TILING["gn"], "the sub-tile count is untouched"


def test_patching_anything_but_a_drain_is_refused():
    """A stage whose tail moved would otherwise be re-encoded as a DRAIN."""
    with pytest.raises(ValueError, match="not a DRAIN"):
        _remote(ISA.gemm(gm=8, gn=8, nk=2), (0, 1), 2, (2, 2))


def test_a_sharded_value_needs_one_part_per_rank(two, operands):
    a, _, _ = operands
    with pytest.raises(ValueError, match="needs 2 parts"):
        Sharded(two, COPIED, [two[0].tensor(a)], (M, K_))


def test_a_step_records_where_it_runs_and_not_when():
    """A Step is placement; `MeshGroup.run` is the only thing that sequences."""
    step = Step(2, O.matmul, (), {"gm": 8}, node=(1, 1))
    assert (step.rank, step.node, step.knobs) == (2, (1, 1), {"gm": 8})


def matmul_on(dev, a, b):
    """One matmul on `dev` at the prototypes' tiling."""
    return O.matmul(dev.tensor(a), dev.tensor(b), **TILING)


def _cluster(unit) -> bool:
    return hasattr(unit, "acc")
