"""The mover's command words, field by field against `mm_mover.v`'s decode.

A register write that is one bit wide too far still writes a legal command, and
the engine reports success on it. These check the ENCODING against the decode in
`mm_mover.v` and `mx_tdesc.v`, field by field, and nothing else.

The words themselves are checked end to end elsewhere: `tests/sysnode/
mover_cfg32_tb.v` replays a program like these into a real mover through the
ship's control port and verifies all 64 moved words. Nothing here has run on
silicon.
"""

import pytest
from kohakuaccel.device import mover as MV


def word(writes, offset, at=0):
    """The `at`-th write to `offset`."""
    got = [v for off, v in writes if off == offset]
    assert len(got) > at, f"no write {at} to {offset:#x}"
    return got[at]


def bits(value, hi, lo):
    return (value >> lo) & ((1 << (hi - lo + 1)) - 1)


def test_transpose_is_refused_and_points_at_the_descriptor():
    """Mode 1 is a canned convenience, not a capability the engine lacks.

    Every index permutation is affine, so a transpose is COPY with the two
    walkers in different orders. The refusal has to SAY that, because "mode 1 is
    missing" reads as a hardware gap and sends someone to ask for RTL.
    """
    with pytest.raises(MV.MoverError, match="COPY"):
        MV.program(MV.TRANSPOSE, MV.Walker(0, [(4, 32)]), MV.Walker(0, [(4, 32)]))


def test_a_transpose_is_expressible_as_a_copy_today():
    """The thing mode 1 would have canned, built by hand out of two walkers.

    A (rows, cols) transpose: the destination walks its own order and the source
    walks the same index space with the two loop levels swapped. Both fit six
    levels with four to spare, and nothing about it needs a mode.
    """
    rows, cols, wb = 8, 16, 32
    src = MV.Walker(0x1000, [(rows, wb), (cols, rows * wb)])
    dst = MV.Walker(0x2000, [(rows, cols * wb), (cols, wb)])
    writes = MV.copy(src, dst)
    assert writes[-1][1] & 0x7 == MV.COPY
    strides = [bits(v, 51, 20) for off, v in writes if off == MV.R_DIM]
    assert strides == [wb, rows * wb, cols * wb, wb], "the two walks are not swapped"


def test_go_is_the_last_write_and_carries_the_mode():
    """GO is a one-cycle pulse, so a descriptor written after it is too late."""
    writes = MV.copy(MV.Walker(0x1000, [(8, 32)]), MV.Walker(0x2000, [(8, 32)]))
    assert writes[-1][0] == MV.R_CTRL
    ctrl = writes[-1][1]
    assert bits(ctrl, 2, 0) == MV.COPY
    assert bits(ctrl, 4, 3) == MV.W16
    assert bits(ctrl, 16, 16) == 1
    assert MV.R_CTRL not in [off for off, _ in writes[:-1]]


def test_the_header_carries_the_base_at_bit_four_and_the_rank_at_44():
    """`8'h10: d_base <= cfg_data[4 +: ADDR_W]; d_ndim <= cfg_data[46:44]`."""
    # 32-byte aligned, and still lights a bit in every nibble of the field.
    writes = MV.Walker(0x9_8765_4320, [(2, 32), (4, 64)]).program(1)
    hdr = word(writes, MV.R_HDR)
    assert bits(hdr, 0, 0) == 1, "walker select is bit 0"
    assert bits(hdr, 43, 4) == 0x9_8765_4320
    assert bits(hdr, 46, 44) == 2


def test_a_dimension_is_staged_then_latched():
    """`d_dim_en` is pulsed by the 0x20 write, so 0x18 alone loads nothing."""
    writes = MV.Walker(0, [(3, 32), (5, -64)]).program(0)
    order = [off for off, _ in writes]
    assert order == [
        MV.R_HDR,
        MV.R_DIM,
        MV.R_AXIS,
        MV.R_DIM,
        MV.R_AXIS,
        MV.R_WINDOW,
        MV.R_WINDOW,
    ]
    outer = word(writes, MV.R_DIM, 0)
    assert bits(outer, 3, 1) == 0, "the first dimension given is index 0"
    assert bits(outer, 19, 4) == 3
    inner = word(writes, MV.R_DIM, 1)
    assert bits(inner, 3, 1) == 1
    assert bits(inner, 19, 4) == 5
    assert bits(inner, 51, 20) == (-64) & 0xFFFF_FFFF, "the stride is signed"


def test_the_dimensions_are_outermost_first():
    """`mx_tdesc` makes the HIGHEST live index the innermost loop.

    That is the opposite of `vec_agu`, where dimension 0 is the fastest. A list
    written the vector core's way walks the loops inside out, which is a legal
    descriptor over a plausible wrong tensor.
    """
    rows, cols = (4, 1024), (8, 32)
    writes = MV.Walker(0, [rows, cols]).program(0)
    assert bits(word(writes, MV.R_DIM, 0), 51, 20) == 1024, "index 0 is the outer"
    assert bits(word(writes, MV.R_DIM, 1), 51, 20) == 32


def test_a_bound_axis_is_numbered_one_and_two():
    padded = MV.Walker(0, [(4, 32)], window={2: (-1, 66)}).program(1)
    got = word(padded, MV.R_WINDOW, 1)
    assert bits(got, 0, 0) == 1
    assert bits(got, 1, 1) == 1, "axis 2 is the second bound axis"
    assert bits(got, 17, 2) == (-1) & 0xFFFF
    assert bits(got, 33, 18) == 66


def test_an_unwindowed_walker_still_writes_both_extents_as_zero():
    """A descriptor must not inherit the last move's bounds.

    `d_aext` is written only by 0x28 and cleared only by reset (`mx_tdesc.v:102`,
    `:80`). An omitted register leaves a previous window standing, and a stale
    extent makes `valid` low -- which `mm_mover.v:271` turns into K_SKIP, so the
    write is suppressed while the walk completes and the move reports done, no
    fault, no bytes moved. Extent 0 disables the axis.
    """
    plain = MV.Walker(0, [(4, 32)]).program(0)
    windows = [v for off, v in plain if off == MV.R_WINDOW]
    assert len(windows) == 2, "both bound axes are stated"
    for at, got in enumerate(windows):
        assert bits(got, 1, 1) == at, "axis 1 then axis 2"
        assert bits(got, 33, 18) == 0, "extent 0 disables the axis"
        assert bits(got, 17, 2) == 0

    # A walker that states one axis still clears the other.
    one = MV.Walker(0, [(4, 32)], window={1: (0, 8)}).program(0)
    ext = [bits(v, 33, 18) for off, v in one if off == MV.R_WINDOW]
    assert ext == [8, 0]


def test_a_misaligned_base_or_stride_is_refused_on_both_walkers():
    """The hardware only faults the DESTINATION.

    `lt_ma = |dst_addr[4:0]` raises F_ALIGN, but nothing checks the source: a
    misaligned one emits an illegal AXI burst instead. So it is refused here or
    it is refused nowhere.
    """
    for sel in (0, 1):
        with pytest.raises(MV.MoverError, match="aligned"):
            MV.Walker(MV.WORD_BYTES + 8, [(4, 32)]).program(sel)
        with pytest.raises(MV.MoverError, match="multiple of"):
            MV.Walker(0, [(4, 8)]).program(sel)
    # A stride that only goes off alignment on a later axis is still refused.
    with pytest.raises(MV.MoverError, match="multiple of"):
        MV.Walker(0, [(4, 32), (4, 40)]).program(0)
    MV.Walker(MV.WORD_BYTES * 3, [(4, 32), (8, -64)]).program(0)


def test_a_value_that_does_not_fit_is_refused():
    """The one failure this encoding cannot report on its own."""
    for bad in (MV.Walker(1 << 40, [(1, 32)]), MV.Walker(0, [(1 << 16, 32)])):
        with pytest.raises(MV.MoverError):
            bad.program(0)
    with pytest.raises(MV.MoverError, match="F_EWIDTH"):
        MV.program(MV.COPY, ewidth=3, dst=MV.Walker(0, [(1, 32)]))


def test_a_walker_states_between_one_and_six_dimensions():
    with pytest.raises(MV.MoverError):
        MV.Walker(0, []).program(0)
    with pytest.raises(MV.MoverError):
        MV.Walker(0, [(2, 32)] * 7).program(0)


def test_status_decodes_the_fault_and_names_the_mode_refusal():
    got = MV.status((7 << 40) | (3 << 24) | (5 << 8) | (4 << 4) | 1)
    assert got["busy"] and got["moves"] == 7 and got["reads"] == 3
    assert got["writes"] == 5 and got["fault_code"] == 4
    assert "TRANSPOSE" in got["fault"]


def test_an_offset_outside_the_mover_half_is_refused():
    """`AUX_CFG` splits at 0x80: above it is the interlink's, not the mover's."""

    class Fake:
        def __init__(self):
            self.seen = []

        def write64(self, addr, value):
            self.seen.append((addr, value))

    t = Fake()
    MV.issue(t, MV.fill(MV.Walker(0x400, [(4, 32)]), 0xABCD), base=0x8000)
    assert t.seen[0][0] == 0x8000 + MV.AUX_CFG + MV.R_HDR
    assert t.seen[-1][0] == 0x8000 + MV.AUX_CFG + MV.R_CTRL
    with pytest.raises(MV.MoverError):
        MV.issue(t, [(MV.AUX_CLIENT_SPAN, 0)], base=0x8000)
