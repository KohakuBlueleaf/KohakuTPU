"""Changing the mesh clock from the host.

`src/ktpu/hw/clock.py` is the arithmetic -- which dividers produce which
frequency, and which ones the MMCM will accept. This is the transport half: how
those dividers reach the Clocking Wizard, and how to tell that they landed.

THE WIZARD IS 32-BIT AND THE TRANSPORT IS 64-BIT. `LOAD` at 0x25C is the upper
half of the word at 0x258, so a write is read-modify-write; a bare 64-bit write
would clear the register beside it, and the symptom is a clock that never
changes -- indistinguishable from a wrong base address.

From docs/arch/physical/clocking.md: a retune RESETS EVERY MESH (memory survives,
being on its own clock), the interlink must be quiesced first because it is
credit-based, and the built-in frequency is the verified ceiling.
"""

import time

from kohakuaccel.transport.base import MASK32, Transport

from ktpu.hw.clock import (
    DEFAULT_D,
    DEFAULT_K,
    REG_CLKCFG0,
    REG_CLKCFG2,
    REG_STATUS,
    STATUS_LOCKED,
    ClockError,
    ClockSetting,
    registers,
    solve,
)

#: Offsets from the control window a second AXI4-Lite slave usually lands on.
#: The address editor decides; `find_wizard` verifies rather than trusts.
NEARBY = (
    0x1_0000,
    0x2_0000,
    0x1000,
    0x10_0000,
    -0x1_0000,
    0x4000,
    0x8000,
    0x20_0000,
)

#: Windows to try when the control window is not known.
CANDIDATES = (
    0x4_0081_0000,
    0x4_0001_0000,
    0x8001_0000,
    0x1001_0000,
    0x4_0080_1000,
    0x0001_0000,
)


def candidates_near(control: int) -> tuple:
    """Windows worth trying, given where the control agent answered.

    A block design almost always assigns a second lite slave a short hop from
    the first, so scanning outward from the one window that IS known beats
    guessing absolute addresses.
    """
    return tuple(control + off for off in NEARBY) + CANDIDATES


def read32(transport: Transport, addr: int) -> int:
    """One 32-bit register, from whichever half of the 64-bit word holds it."""
    word = transport.read64(addr & ~7)
    return (word >> (32 if addr & 4 else 0)) & MASK32


def write32(transport: Transport, addr: int, value: int) -> None:
    """One 32-bit register, leaving the register beside it alone."""
    base = addr & ~7
    word = transport.read64(base)
    if addr & 4:
        word = (word & MASK32) | ((value & MASK32) << 32)
    else:
        word = (word & (MASK32 << 32)) | (value & MASK32)
    transport.write64(base, word)


def decode(cfg0: int, cfg2: int) -> ClockSetting:
    """The setting a wizard is currently holding, from its two config words."""
    return ClockSetting(m=(cfg0 >> 8) & 0xFFFF, d=cfg0 & 0xFF, k=cfg2 & 0xFF)


def probe(transport: Transport, base: int) -> ClockSetting | None:
    """The setting at `base`, or None if nothing there looks like a wizard.

    Validation is the MMCM's own limits: a legal multiplier, a legal input
    divider, and a VCO inside the part's range. Random memory almost never
    decodes to all three, and a wrong base that DOES decode would have produced
    the frequency the board is running at, which the caller can check.
    """
    # TransportUnavailable is NOT "no wizard here" -- swallowing it reports a dead
    # link as a failed decode, which cost hours on 2026-08-12.
    got = decode(
        read32(transport, base + REG_CLKCFG0), read32(transport, base + REG_CLKCFG2)
    )
    try:
        got.check()
    except ClockError:
        return None
    return got


def find_wizard(transport: Transport, candidates=None, control=None) -> int | None:
    """The first candidate window whose contents decode to a legal setting.

    With `control` given, scans outward from there first. Returns None rather
    than guessing: a wrong base writes a divider into a status register, and the
    clock then silently never changes.
    """
    if candidates is None:
        candidates = candidates_near(control) if control is not None else CANDIDATES
    for base in candidates:
        if probe(transport, base) is not None:
            return base
    return None


def locked(transport: Transport, base: int) -> bool:
    """Whether the MMCM has locked since the last load."""
    return bool(read32(transport, base + REG_STATUS) & STATUS_LOCKED)


def current(transport: Transport, base: int) -> ClockSetting:
    """What the wizard is set to right now."""
    got = probe(transport, base)
    if got is None:
        raise ClockError(
            f"nothing at {base:#x} decodes as a Clocking Wizard configuration; "
            f"a wrong base writes a divider into a status register and the clock "
            f"silently never changes"
        )
    return got


def set_mhz(
    transport: Transport,
    base: int,
    mhz: float,
    d: int = DEFAULT_D,
    k: int = DEFAULT_K,
    timeout: float = 2.0,
) -> ClockSetting:
    """Retune the mesh clock to at most `mhz`, and wait for lock.

    `k` is the output divider and it BOUNDS THE RANGE: at the default the lowest
    legal output is 200 MHz, so asking for 100 MHz needs `k=8`. Raises
    :class:`ClockError` if the target is unreachable at this `(d, k)`, or if the
    MMCM does not lock within `timeout`.

    Returns the setting actually applied, which is the closest legal one AT OR
    BELOW the request -- never above, so a sweep upward cannot step past the
    frequency being hunted.
    """
    want = solve(mhz, d=d, k=k)
    for offset, value in registers(want):
        write32(transport, base + offset, value)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if locked(transport, base):
            return want
        time.sleep(0.01)
    raise ClockError(
        f"the MMCM did not lock within {timeout}s after loading "
        f"M={want.m} D={want.d} k={want.k} ({want.mhz} MHz). The mesh is now on "
        f"an unlocked clock: hold it in reset and reload a known-good setting."
    )
