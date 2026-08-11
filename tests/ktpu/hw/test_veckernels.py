"""The vector kernels, and the L1 footprint band that silently corrupts output.

The band is the only thing here that is not derivable from the source: it was
MEASURED on ship_3x2 and has no explanation yet, so it is pinned by test rather
than by argument. If a later bitstream makes 352..480 words work, these are the
tests that should be changed -- deliberately, with a fresh measurement.
"""

import pytest

from ktpu.hw import veckernels as K


# ---- the measured band -----------------------------------------------------
@pytest.mark.parametrize("words", [8, 128, 320, 512])
def test_a_footprint_at_either_end_is_allowed(words):
    K.require_l1("probe", words)


@pytest.mark.parametrize("words", [321, 352, 416, 480, 511])
def test_a_footprint_inside_the_band_is_refused(words):
    """In the band the card completes, signals success and returns wrong data."""
    with pytest.raises(ValueError, match="measured bad band"):
        K.require_l1("probe", words)


def test_more_than_l1_is_refused_as_capacity_not_as_the_band():
    with pytest.raises(ValueError, match="L1 has 512"):
        K.require_l1("probe", 513)


# ---- the kernels apply it --------------------------------------------------
@pytest.mark.parametrize("chunks", [11, 12, 13, 14, 15])
def test_a_three_input_map_in_the_band_is_refused(chunks):
    """Measured wrong on the card at exactly these chunk counts."""
    with pytest.raises(ValueError, match="measured bad band"):
        K.MapKernel("affine", chunks)


@pytest.mark.parametrize("chunks", [4, 6, 8, 10, 16])
def test_a_three_input_map_outside_the_band_builds(chunks):
    k = K.MapKernel("affine", chunks)
    assert (k.nin + 1) * k.bw <= K.L1_WORDS


@pytest.mark.parametrize("nelem", [1408, 1536, 1664, 1792, 1920])
def test_a_group_norm_in_the_band_is_refused(nelem):
    """1408..1920 all returned wrong data; 1280 and 2048 did not."""
    with pytest.raises(ValueError, match="measured bad band"):
        K.group_norm_kernel(nelem, nelem // 128)


@pytest.mark.parametrize("nelem", [640, 1024, 1280, 2048])
def test_a_group_norm_outside_the_band_builds(nelem):
    image, static, batch = K.group_norm_kernel(nelem, nelem // 128)
    assert image and static and callable(batch)


def test_the_widest_safe_group_is_1280_or_2048_elements():
    """At SDXL's C=320 and 32 groups this is what caps the spatial extent: a
    group is 10*hw elements, so hw <= 128."""
    ok = [n for n in range(128, 2177, 128)
          if _builds(K.group_norm_kernel, n, n // 128)]
    assert ok[-1] == 2048
    assert [n for n in ok if n < 2048][-1] == 1280


def _builds(fn, *a):
    try:
        fn(*a)
        return True
    except ValueError:
        return False


# ---- the kernels themselves ------------------------------------------------
def test_every_map_body_names_its_input_count():
    for name, (nin, _) in K.BODIES.items():
        k = K.MapKernel(name, 4)
        assert k.nin == nin, name


def test_a_map_kernel_is_one_fill_per_input_and_one_drain():
    k = K.MapKernel("add", 8)
    descs = k.static_descs()
    assert len(k.ad_fill) == 2
    assert k.ad_drain == 5 and descs


def test_group_norm_reads_x_three_times_for_two_passes_and_a_normalise():
    """E[x^2]-E[x]^2 would need one pass; the lane carries 16 significand bits
    and that identity cancels exactly where a normalisation uses it."""
    image, _, _ = K.group_norm_kernel(1024, 8)
    from ktpu.hw import vector as V

    vlds = sum(1 for w in image if (w >> 27) & 0x1F == V.OPS["VLD"])
    assert vlds == 8 + 8 + 3 * 8  # x, x again, then x/gamma/beta per chunk


def test_flash_rows_refuses_a_block_that_will_not_fit():
    with pytest.raises(ValueError):
        K.flash_rows_kernel(64, 256, 1.0)
