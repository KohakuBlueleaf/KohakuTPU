"""The tile chooser, and the regression it exists to prevent.

The decisive test is `test_the_tile_does_not_depend_on_the_cluster_count`: the
legacy driver chose the tile from `n // clusters`, and that single substitution
turned a good tile into one as tall as the whole problem. Every other test here
pins a ranking property that was arrived at by measurement.
"""

import pytest

from ktpu.ir.sched import ScheduleError
from ktpu.passes.tile import choose_tile, grid_for, occupancy
from ktpu.target import VU13P_8CU as T
from ktpu.target import Target


def test_the_tile_does_not_depend_on_the_cluster_count():
    """THE REGRESSION TEST. `choose_tile` sees the whole problem, so the answer
    is the same however many clusters will run it -- and it is the good tile,
    not the 256x32x32 the legacy driver picked from a 32-wide band."""
    good = choose_tile(256, 1024, 256, T)
    assert (good.gm, good.gn, good.nk) == (16, 32, 4)
    assert (good.tile.m, good.tile.k, good.tile.n) == (64, 128, 128)

    for clusters in (1, 2, 4, 8):
        t = Target(clusters=clusters)
        c = choose_tile(256, 1024, 256, t)
        assert (c.gm, c.gn, c.nk) == (16, 32, 4), clusters


def test_the_wide_reference_gets_the_same_tile():
    """512x256x1024, the recorded 8-cluster benchmark."""
    c = choose_tile(512, 256, 1024, T)
    assert (c.gm, c.gn, c.nk) == (16, 32, 4)


def test_gm_gn_count_subtiles_not_elements():
    """The factor that is easy to drop, and that this project has already
    dropped once in a document: gm=16, gn=32 is a 64 x 128 block, not 16 x 32."""
    c = choose_tile(1024, 1024, 1024, T)
    assert c.tile.m == c.gm * T.lanes
    assert c.tile.n == c.gn * T.lanes
    assert c.tile.k == c.nk * T.kblock


def test_intensity_beats_area():
    """32x1 and 8x4 both hold 32 sub-tiles; their intensities are 1.94 and 5.33,
    so the chooser must never be maximising gm*gn."""
    small = Target(tiles=32, l1_a=256, l1_b=256)
    c = choose_tile(4096, 4096, 4096, small)
    assert c.gm * c.gn <= 32
    assert c.intensity > 4.0


def test_padding_discount_rejects_a_tile_the_problem_cannot_fill():
    """A 300x300 GEMM at 64x128 pads to 320x384 and does 36.5% more arithmetic
    than the problem contains, so the score must fall for it."""
    wide = choose_tile(300, 1024, 300, T)
    assert wide.useful < 1.0
    # ...and the chooser should have moved to something that wastes less than
    # the biggest tile would.
    biggest = choose_tile(4096, 4096, 4096, T)
    assert wide.tile.n <= biggest.tile.n


def test_a_problem_that_fits_exactly_wastes_nothing():
    c = choose_tile(1024, 1024, 1024, T)
    assert c.useful == pytest.approx(1.0)


def test_nk_respects_the_double_buffer():
    """A chunk may use only half of L1 A: the sweep for chunk i reads while
    chunk i+1 fills, and the overlap was 22.3% of the machine's time."""
    c = choose_tile(1024, 4096, 1024, T)
    assert c.nk <= T.l1_a // (T.l1_a_banks * c.gm)
    assert c.nk <= T.l1_b // (T.l1_a_banks * c.gn)


@pytest.mark.parametrize("l1", (256, 512, 1024))
@pytest.mark.parametrize(
    "m,k,n",
    [
        (64, 288, 256),  # off by 8.2e+02 on the card when gn*nk reached 288
        (64, 320, 256),
        (64, 320, 320),  # off by 3.5e+03
        (4096, 320, 320),
        (1024, 640, 5120),
        (256, 2560, 1280),
    ],
)
def test_a_chunk_never_outgrows_one_l1_bank(m, k, n, l1):
    """`aoff`/`boff`/`eoff` are 8-bit offsets WITHIN a bank and the sweep's own
    `boff + h*nk + kb` is 8 bits, so a chunk of over 256 entries wraps and the
    sub-tiles past the wrap read another block's operand -- silently."""
    t = Target(l1_a=l1, l1_b=l1)
    c = choose_tile(m, k, n, t)
    bank = l1 // t.l1_a_banks
    assert c.gm * c.nk <= bank, f"A chunk {c.gm * c.nk} entries wraps"
    assert c.gn * c.nk <= bank, f"B chunk {c.gn * c.nk} entries wraps"
    assert c.gn * c.nk <= 256, "no offset field can reach past 256"


def test_nk_never_exceeds_the_problems_k():
    c = choose_tile(256, 32, 256, T)  # one K block
    assert c.nk == 1


def test_no_tile_fits_is_an_error_not_a_silent_default():
    with pytest.raises(ScheduleError, match="no tile fits"):
        choose_tile(256, 256, 256, Target(l1_a=1, l1_b=1, tiles=1))


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------


def test_the_bad_shape_gets_eight_tiles_for_eight_clusters():
    """4 x 2 = 8, exactly saturated at 8 clusters -- enough to fill them with no
    slack. That is the honest number; an earlier doc claimed 128."""
    c = choose_tile(256, 1024, 256, T)
    g = grid_for(256, 256, c)
    assert (g.m, g.n, g.sk) == (4, 2, 1)
    assert g.size == 8
    assert occupancy(g, T) == (8, 8)


def test_the_wide_reference_has_plenty_of_tiles():
    c = choose_tile(512, 256, 1024, T)
    g = grid_for(512, 1024, c)
    assert g.size == 64
    assert occupancy(g, T) == (8, 8)


def test_a_skinny_deep_shape_starves_at_any_2d_grid():
    """64x4096x64 is ONE tile, so seven of eight clusters get nothing however
    the 2D grid is arranged. This is the shape that needs the third dimension,
    and the reason optimization.md J3's occupancy argument survives at all."""
    c = choose_tile(64, 4096, 64, T)
    g = grid_for(64, 64, c)
    assert g.size == 1
    assert occupancy(g, T) == (1, 8)

    split = grid_for(64, 64, c, sk=8)
    assert split.size == 8


def test_the_grid_covers_a_padded_problem():
    c = choose_tile(300, 1024, 300, T)
    g = grid_for(300, 300, c)  # covers() runs inside, and must not raise
    assert g.m * c.tile.m >= 300
    assert g.n * c.tile.n >= 300


def test_grid_for_reports_a_tile_that_cannot_cover():
    """grid_for runs covers(), so a miscomputed grid fails here rather than
    producing a program that leaves part of C untouched."""
    c = choose_tile(256, 1024, 256, T)
    with pytest.raises(ScheduleError, match="uncovered"):
        # A grid built for a smaller problem than the one it is checked against.
        grid_for(256, 256, c).covers(512, 256, c.tile)
