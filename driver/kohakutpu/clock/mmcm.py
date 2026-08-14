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

#: Three words per output -- divide, phase, duty. CLKOUT6's three end at 0x258,
#: immediately below LOAD, which is the check that the stride is right.
OUT_MAX = 6
OUT_STRIDE = 0x0C

#: ONLY CLKOUT0's OFFSET IS PROVEN; the shipped design drives one output. The
#: rest MUST be verified at bring-up, or a divider lands in a phase register.
OUTPUTS_VERIFIED = 1


def reg_clkout(n: int) -> int:
    """Offset of output `n`'s divide register."""
    if not 0 <= n <= OUT_MAX:
        raise ClockError(f"CLKOUT{n} does not exist; an MMCME4 has 0..{OUT_MAX}")
    return REG_CLKCFG2 + n * OUT_STRIDE


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


@dataclass(frozen=True)
class MultiSetting:
    """One MMCM configuration across CLKOUT0..CLKOUT`n-1`.

    `m` and `d` are shared because there is ONE VCO. `ks` is the only per-output
    freedom and every output is VCO/k, so the achievable spread is integer
    ratios and nothing else; independent rates need a second MMCM.
    """

    m: int
    d: int
    ks: tuple[int, ...]

    @property
    def pfd_mhz(self) -> float:
        return FIN_MHZ / self.d

    @property
    def vco_mhz(self) -> float:
        return self.pfd_mhz * self.m

    def mhz(self, n: int = 0) -> float:
        """Output `n`'s frequency."""
        return self.vco_mhz / self.ks[n]

    def check(self) -> None:
        """Raise ClockError unless every MMCME4 limit holds, on every output."""
        if not self.ks:
            raise ClockError("a setting with no outputs drives nothing")
        if len(self.ks) > OUT_MAX + 1:
            raise ClockError(f"{len(self.ks)} outputs; an MMCME4 has {OUT_MAX + 1}")
        # The shared half is identical for every output, so check it once
        # through the single-output type rather than restating its limits.
        for k in self.ks:
            ClockSetting(m=self.m, d=self.d, k=k).check()


def solve_multi(
    target_mhz: float,
    divisors: tuple[int, ...] = (1,),
    d: int = DEFAULT_D,
    k: int = DEFAULT_K,
) -> MultiSetting:
    """The legal multi-output setting closest to `target_mhz` at or below it.

    `target_mhz` is CLKOUT0's; output `n` runs at CLKOUT0 divided by
    `divisors[n]`, which is what "compute units at half the fabric rate" means
    on one VCO. `divisors[0]` must be 1. Raises ClockError if any output's
    divider leaves the MMCM's range, which a large divisor does long before the
    frequency looks unreasonable.
    """
    if not divisors or divisors[0] != 1:
        raise ClockError(f"divisors[0] is CLKOUT0 itself and must be 1, got {divisors}")
    base = solve(target_mhz, d=d, k=k)
    out = MultiSetting(m=base.m, d=base.d, ks=tuple(base.k * v for v in divisors))
    out.check()
    return out


def registers_multi(s: MultiSetting) -> list[tuple[int, int]]:
    """The (offset, value) writes that put `s` into a Clocking Wizard.

    Divides only: phase and duty keep whatever the Wizard was built with, which
    is what the single-output path has always done. LOAD is last, or a
    half-updated configuration is applied.
    """
    s.check()
    ops = [(REG_CLKCFG0, (s.m << 8) | s.d)]
    ops += [(reg_clkout(n), kn) for n, kn in enumerate(s.ks)]
    ops.append((REG_LOAD, 0x3))
    return ops
