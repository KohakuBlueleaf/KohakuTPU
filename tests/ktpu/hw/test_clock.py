"""The mesh clock solver, against MMCME4's limits.

Every setting this produces must be legal, and the sweep must be monotone with
a uniform step -- that is what makes "walk M up until the answers break" a
measurement rather than a guess.
"""

import pytest

from ktpu.hw import clock


def test_the_default_configuration_covers_200_to_400_mhz():
    ss = clock.settings()
    assert ss[0].mhz == pytest.approx(200.0)
    assert ss[-1].mhz == pytest.approx(400.0)


def test_one_step_of_m_is_6_25_mhz():
    assert clock.step_mhz() == pytest.approx(6.25)
    ss = clock.settings()
    steps = {round(b.mhz - a.mhz, 6) for a, b in zip(ss, ss[1:])}
    assert steps == {6.25}, "a non-uniform step makes a sweep unreadable"


def test_every_produced_setting_is_legal():
    for s in clock.settings():
        s.check()
        assert clock.VCO_MIN_MHZ <= s.vco_mhz <= clock.VCO_MAX_MHZ
        assert clock.PFD_MIN_MHZ <= s.pfd_mhz <= clock.PFD_MAX_MHZ


def test_the_pfd_is_25_mhz_at_the_default_divider():
    assert clock.settings()[0].pfd_mhz == pytest.approx(25.0)


@pytest.mark.parametrize(
    "target,expect",
    [(300.0, 300.0), (301.0, 300.0), (299.9, 293.75), (400.0, 400.0), (200.0, 200.0)],
)
def test_solve_never_overshoots(target, expect):
    """Overshooting would step past the frequency being hunted."""
    assert clock.solve(target).mhz == pytest.approx(expect)


def test_a_target_below_the_range_is_an_error_not_a_clamp():
    with pytest.raises(clock.ClockError, match="below the lowest legal"):
        clock.solve(100.0)


def test_d_1_gives_a_25_mhz_step_over_the_same_range():
    ss = clock.settings(d=1)
    assert ss[0].mhz == pytest.approx(200.0)
    assert ss[-1].mhz == pytest.approx(400.0)
    assert clock.step_mhz(d=1) == pytest.approx(25.0)


def test_a_low_pfd_truncates_the_top_of_the_range():
    """M maxes at 128, so PFD 10 MHz cannot reach a 1600 MHz VCO."""
    ss = clock.settings(d=10)
    assert ss[-1].mhz == pytest.approx(320.0)


def test_an_illegal_setting_names_which_limit_it_broke():
    with pytest.raises(clock.ClockError, match="VCO"):
        clock.ClockSetting(m=2, d=4, k=4).check()
    with pytest.raises(clock.ClockError, match="PFD"):
        clock.ClockSetting(m=64, d=100, k=4).check()


def test_the_register_sequence_loads_last():
    """A LOAD before both dividers applies a half-updated configuration."""
    regs = clock.registers(clock.solve(300.0))
    assert [off for off, _ in regs] == [
        clock.REG_CLKCFG0,
        clock.REG_CLKCFG2,
        clock.REG_LOAD,
    ]
    (_, cfg0), (_, cfg2), _ = regs
    assert cfg0 & 0xFF == 4, "D in the low byte"
    assert cfg0 >> 8 == 48, "300 MHz is 25 MHz PFD x 48 / 4"
    assert cfg2 == 4
