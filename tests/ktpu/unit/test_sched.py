"""Level-2 invariants.

The coverage check and the split-K check are the two that matter: each one
catches a class of bug that produces a program which RUNS and returns a wrong or
partial answer, which is the worst failure mode available.
"""

import pytest

from ktpu.ir import FP16, OpKind
from ktpu.ir.sched import (
    Band,
    Engine,
    Grid,
    SchedOp,
    Schedule,
    ScheduleError,
    Tile,
    split_k_needed,
)

# ---------------------------------------------------------------------------
# Coverage: the cheapest real test in the system
# ---------------------------------------------------------------------------


def test_grid_covers_an_exact_fit():
    """256x1024x256 with the good tile: 4 x 2 = 8 instances, one per cluster.

    This is the shape the legacy driver ran on one cluster at a time.
    """
    Grid(m=4, n=2).covers(out_m=256, out_n=256, tile=Tile(m=64, n=128, k=128))


def test_grid_covers_with_padding():
    """Overshoot below one tile is padding, and padding is fine: a zero
    contributes nothing to a dot product."""
    Grid(m=4, n=2).covers(out_m=250, out_n=200, tile=Tile(m=64, n=128))


def test_gap_in_rows_is_caught():
    with pytest.raises(ScheduleError, match="rows uncovered"):
        Grid(m=3, n=2).covers(out_m=256, out_n=256, tile=Tile(m=64, n=128))


def test_gap_in_columns_is_caught():
    with pytest.raises(ScheduleError, match="columns uncovered"):
        Grid(m=4, n=1).covers(out_m=256, out_n=256, tile=Tile(m=64, n=128))


def test_a_whole_wasted_instance_is_caught():
    """Overshoot by a full tile means some instance has no work -- a scheduling
    mistake, not a rounding one."""
    with pytest.raises(ScheduleError, match="no work"):
        Grid(m=6, n=2).covers(out_m=256, out_n=256, tile=Tile(m=64, n=128))


def test_grid_size_counts_the_k_split():
    assert Grid(m=4, n=2, sk=1).size == 8
    assert Grid(m=4, n=2, sk=4).size == 32


def test_instances_enumerate_every_triple():
    got = set(Grid(m=2, n=2, sk=2).instances())
    assert len(got) == 8
    assert (1, 1, 1) in got


# ---------------------------------------------------------------------------
# Split-K must not be emitted without its reduction
# ---------------------------------------------------------------------------


def _mm(sk: int) -> Band:
    return Band(
        engine=Engine.MATMUL,
        grid=Grid(m=4, n=2, sk=sk),
        tile=Tile(m=64, n=128, k=128),
        ops=[SchedOp(OpKind.MATMUL)],
        name="mm",
    )


def test_plain_matmul_band_verifies():
    s = Schedule()
    s.add(_mm(sk=1))
    s.verify()


def test_split_k_without_a_reduction_is_rejected():
    """The failure that would look like a working program and return a
    fraction of the answer."""
    s = Schedule()
    s.add(_mm(sk=4))
    with pytest.raises(ScheduleError, match="nothing reduces its partials"):
        s.verify()


def test_split_k_with_a_vector_reduction_verifies():
    s = Schedule()
    s.add(_mm(sk=4))
    s.add(
        Band(
            engine=Engine.VECTOR,
            grid=Grid(m=8),
            tile=Tile(m=512),
            ops=[SchedOp(OpKind.ADD)],
            reduces="mm",
            name="epilogue",
        )
    )
    s.verify()


def test_reducing_on_the_matmul_engine_is_rejected():
    """The vector core is the only engine with the range to finish it."""
    s = Schedule()
    s.add(_mm(sk=4))
    s.add(
        Band(
            engine=Engine.MATMUL,
            grid=Grid(m=8),
            tile=Tile(m=512),
            reduces="mm",
            name="wrong",
        )
    )
    with pytest.raises(ScheduleError, match="must be the vector core"):
        s.verify()


def test_reducing_an_unknown_band_is_rejected():
    s = Schedule()
    s.add(Band(Engine.VECTOR, Grid(m=1), Tile(m=8), reduces="nope", name="r"))
    with pytest.raises(ScheduleError, match="unknown band"):
        s.verify()


# ---------------------------------------------------------------------------
# Engines, and the formats they actually compute in
# ---------------------------------------------------------------------------


def test_each_engine_names_its_own_compute_format():
    assert Engine.MATMUL.compute_dtype.name == "mxfp7"
    assert Engine.MATMUL.accum_dtype.name == "acc24"
    assert Engine.VECTOR.compute_dtype.name == "e8m15"


def test_split_k_is_needed_when_the_result_leaves_fp16():
    """K=2048 over biased operands reaches ~1e5; FP16 stops at 65504."""
    assert split_k_needed(2048, 104_707.0, FP16)
    assert not split_k_needed(1024, 39_296.0, FP16)


# ---------------------------------------------------------------------------
# It has to be printable, because that was the original defect
# ---------------------------------------------------------------------------


def test_schedule_prints_the_grid_and_the_tile():
    """A scheduling decision you cannot print is one you cannot review -- which
    is exactly how the N-only split survived."""
    s = Schedule(name="gemm")
    s.add(_mm(sk=2))
    s.add(Band(Engine.VECTOR, Grid(m=8), Tile(m=512), reduces="mm", name="ep"))
    text = str(s)
    assert "sk=2" in text
    assert "C[64,128]" in text and "A[64,128] @ B[128,128]" in text
    assert "reduces=mm" in text


def test_tile_prints_in_m_k_n_order():
    assert str(Tile(m=64, n=128, k=32)) == "64x32x128"


def test_degenerate_extents_are_rejected_at_construction():
    with pytest.raises(ScheduleError):
        Tile(m=0)
    with pytest.raises(ScheduleError):
        Grid(m=4, sk=0)
