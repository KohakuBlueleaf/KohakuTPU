"""The MMCM arithmetic, without a card.

The physical fact every test here defends: one MMCM has ONE VCO, so several
outputs are several dividers of one number and never several frequencies.
"""

import pytest
from kohakutpu.clock.mmcm import (
    DEFAULT_D,
    DEFAULT_K,
    OUT_DIV_MAX,
    OUT_MAX,
    REG_CLKCFG0,
    REG_CLKCFG2,
    REG_LOAD,
    VCO_MAX_MHZ,
    VCO_MIN_MHZ,
    ClockError,
    MultiSetting,
    reg_clkout,
    registers_multi,
    solve,
    solve_multi,
)


def test_clkout_stride_ends_where_load_begins():
    """Three words per output, so CLKOUT6's three run out at LOAD.

    This is the only check available that the stride is right without a board:
    a wrong stride would not land the last output exactly below LOAD.
    """
    assert reg_clkout(0) == REG_CLKCFG2
    assert reg_clkout(OUT_MAX) + 0x0C == REG_LOAD


def test_no_such_output():
    with pytest.raises(ClockError):
        reg_clkout(OUT_MAX + 1)


def test_one_vco_for_every_output():
    s = solve_multi(300.0, (1, 2, 4))
    assert s.vco_mhz == pytest.approx(s.mhz(0) * s.ks[0])
    assert s.mhz(1) == pytest.approx(s.mhz(0) / 2)
    assert s.mhz(2) == pytest.approx(s.mhz(0) / 4)
    # The shared half really is shared: no per-output m or d exists to differ.
    assert not hasattr(s, "ms")


def test_clkout0_is_the_reference():
    """A multi-output solve agrees with the single-output one on CLKOUT0."""
    one = solve(287.0)
    many = solve_multi(287.0, (1, 2))
    assert (many.m, many.d, many.ks[0]) == (one.m, one.d, one.k)


def test_divisor_zero_is_clkout0_itself():
    with pytest.raises(ClockError):
        solve_multi(300.0, (2, 4))
    with pytest.raises(ClockError):
        solve_multi(300.0, ())


def test_a_large_divisor_leaves_the_range():
    """The output divider saturates long before the frequency looks silly."""
    too_big = OUT_DIV_MAX // DEFAULT_K + 1
    with pytest.raises(ClockError):
        solve_multi(300.0, (1, too_big))


def test_registers_load_last():
    ops = registers_multi(solve_multi(300.0, (1, 2)))
    assert ops[0][0] == REG_CLKCFG0
    assert ops[-1][0] == REG_LOAD
    # One divide word per output, between the shared config and the load.
    assert [o for o, _ in ops[1:-1]] == [reg_clkout(0), reg_clkout(1)]


def test_the_vco_bounds_are_checked_not_the_output():
    """A legal-looking output frequency off an illegal VCO is still illegal."""
    bad = MultiSetting(m=2, d=DEFAULT_D, ks=(DEFAULT_K,))
    assert bad.vco_mhz < VCO_MIN_MHZ
    with pytest.raises(ClockError):
        bad.check()


def test_the_default_configuration_reaches_300():
    s = solve_multi(300.0, (1, 2))
    assert s.mhz(0) == pytest.approx(300.0)
    assert s.mhz(1) == pytest.approx(150.0)
    assert VCO_MIN_MHZ <= s.vco_mhz <= VCO_MAX_MHZ
