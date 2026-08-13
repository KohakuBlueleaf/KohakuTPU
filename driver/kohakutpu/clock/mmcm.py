"""The mesh clock, as a number the host can change.

`docs/arch/physical/clocking.md` is the design. This is the arithmetic and the
register sequence, kept apart from the transport so both are testable without a
card.

The Clocking Wizard's AXI4-Lite interface writes the same MMCM dividers the DRP
does. Register offsets are PG065's; they are checked against the IP at bring-up,
because a wrong offset here writes a divider into a status register and the
clock simply never changes.
"""

from dataclasses import dataclass

#: MMCME4 limits, DS923.
VCO_MIN_MHZ = 800.0
VCO_MAX_MHZ = 1600.0
PFD_MIN_MHZ = 10.0
PFD_MAX_MHZ = 450.0
MULT_MIN = 2
MULT_MAX = 128
DIV_MAX = 106
OUT_DIV_MAX = 128

#: The reference the board actually presents (`system`, 100 MHz).
FIN_MHZ = 100.0

#: PFD 25 MHz: one step of M is 6.25 MHz at k=4, and jitter stays low enough
#: that a sweep measures the design rather than the clock generator.
DEFAULT_D = 4
DEFAULT_K = 4

#: PG065 AXI4-Lite map. SW_RST is write-1-to-reset; LOAD/SEN together latch a
#: new divider set.
REG_SW_RST = 0x000
REG_STATUS = 0x004
REG_CLKCFG0 = 0x200
REG_CLKCFG2 = 0x208
REG_LOAD = 0x25C

STATUS_LOCKED = 0x1


class ClockError(ValueError):
    """A frequency this MMCM cannot produce."""


@dataclass(frozen=True)
class ClockSetting:
    """One legal MMCM configuration, and what it produces.

    `m` is CLKFBOUT_MULT, `d` the input divider, `k` the CLKOUT0 divider.
    `mhz` is the exact output, which is not necessarily the requested one.
    """

    m: int
    d: int
    k: int

    @property
    def pfd_mhz(self) -> float:
        return FIN_MHZ / self.d

    @property
    def vco_mhz(self) -> float:
        return self.pfd_mhz * self.m

    @property
    def mhz(self) -> float:
        return self.vco_mhz / self.k

    def check(self) -> None:
        """Raise ClockError unless every MMCME4 limit holds."""
        if not MULT_MIN <= self.m <= MULT_MAX:
            raise ClockError(f"M={self.m} outside {MULT_MIN}..{MULT_MAX}")
        if not 1 <= self.d <= DIV_MAX:
            raise ClockError(f"D={self.d} outside 1..{DIV_MAX}")
        if not 1 <= self.k <= OUT_DIV_MAX:
            raise ClockError(f"k={self.k} outside 1..{OUT_DIV_MAX}")
        if not PFD_MIN_MHZ <= self.pfd_mhz <= PFD_MAX_MHZ:
            raise ClockError(
                f"PFD {self.pfd_mhz:.3f} MHz outside "
                f"{PFD_MIN_MHZ}..{PFD_MAX_MHZ}; raise Fin or lower D"
            )
        if not VCO_MIN_MHZ <= self.vco_mhz <= VCO_MAX_MHZ:
            raise ClockError(
                f"VCO {self.vco_mhz:.1f} MHz outside "
                f"{VCO_MIN_MHZ}..{VCO_MAX_MHZ} for M={self.m} D={self.d}"
            )


def step_mhz(d: int = DEFAULT_D, k: int = DEFAULT_K) -> float:
    """How far one step of M moves the output."""
    return FIN_MHZ / (d * k)


def settings(d: int = DEFAULT_D, k: int = DEFAULT_K) -> list[ClockSetting]:
    """Every legal setting at this (D, k), ascending by frequency."""
    out = []
    for m in range(MULT_MIN, MULT_MAX + 1):
        s = ClockSetting(m=m, d=d, k=k)
        try:
            s.check()
        except ClockError:
            continue
        out.append(s)
    return sorted(out, key=lambda s: s.mhz)


def solve(target_mhz: float, d: int = DEFAULT_D, k: int = DEFAULT_K) -> ClockSetting:
    """The legal setting closest to `target_mhz`, at or below it.

    At or below deliberately: this exists to walk a clock UP until the design
    breaks, and overshooting the requested frequency would step past the answer.
    Raises ClockError if nothing legal is at or below the target.
    """
    legal = settings(d, k)
    under = [s for s in legal if s.mhz <= target_mhz + 1e-9]
    if not under:
        raise ClockError(
            f"{target_mhz} MHz is below the lowest legal setting "
            f"{legal[0].mhz} MHz at D={d} k={k}"
        )
    return under[-1]


def registers(s: ClockSetting) -> list[tuple[int, int]]:
    """The (offset, value) writes that put `s` into a Clocking Wizard.

    CLKCFG0 carries the input divider and the multiplier, CLKCFG2 the CLKOUT0
    divider, and LOAD latches both. Ordering matters: a LOAD before both
    dividers are written applies a half-updated configuration.
    """
    s.check()
    return [
        (REG_CLKCFG0, (s.m << 8) | s.d),
        (REG_CLKCFG2, s.k),
        (REG_LOAD, 0x3),
    ]
