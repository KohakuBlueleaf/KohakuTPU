"""Read or retune the mesh clock.

    python scripts/py/mesh_clock.py            # what is it now
    python scripts/py/mesh_clock.py 100M       # drop it to 100 MHz
    python scripts/py/mesh_clock.py 300M       # put it back
    python scripts/py/mesh_clock.py --list     # every legal frequency

The output divider is searched, so a bare frequency is enough; `--k` pins it.
Without a frequency nothing is written.

A retune resets every mesh, so quiesce first and re-upload anything on chip.
"""

import argparse

from kohakuaccel.device import find_control_window
from kohakuaccel.transport.jtag import JtagTransport
from kohakutpu.clock import current, find_wizard, locked, set_mhz
from kohakutpu.clock.mmcm import OUT_DIV_MAX, ClockError, settings
from kohakutpu.host import CANDIDATES as HOST_CANDIDATES


def solve_best(target_mhz: float, d: int = 4):
    """The legal setting closest to `target_mhz` from below, over every `k`.

    `kohakutpu.clock.mmcm.solve` fixes `k`; this searches it, which is what a
    bare frequency on the command line means. Raises ClockError if nothing legal
    sits at or below the target.
    """
    under = [
        s
        for k in range(1, OUT_DIV_MAX + 1)
        for s in settings(d, k)
        if s.mhz <= target_mhz + 1e-9
    ]
    if not under:
        raise ClockError(f"{target_mhz} MHz is below every legal setting at D={d}")
    return max(under, key=lambda s: s.mhz)


def megahertz(text: str) -> float:
    """`100M`, `100MHz`, `1.2G` or a bare number, as MHz."""
    s = text.strip().lower().removesuffix("hz")
    scale = {"k": 1e-3, "m": 1.0, "g": 1e3}.get(s[-1:])
    return float(s[:-1]) * scale if scale else float(s)


def show(label: str, s, is_locked: bool) -> None:
    """Print one :class:`ClockSetting` as a line under `label`."""
    print(
        f"  {label}  {s.mhz:.2f} MHz  (M={s.m} D={s.d} k={s.k}, "
        f"VCO {s.vco_mhz:.1f})   locked={is_locked}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("freq", nargs="?", type=megahertz, help="e.g. 100M; omit to read")
    ap.add_argument("--base", type=lambda s: int(s, 0), help="wizard window")
    ap.add_argument("--d", type=int, default=4, help="input divider")
    ap.add_argument("--k", type=int, help="output divider; searched if omitted")
    ap.add_argument("--list", action="store_true", help="every legal frequency")
    args = ap.parse_args()

    if args.list:
        for s in settings(args.d, args.k or 4):
            print(f"  {s.mhz:8.2f} MHz   M={s.m:>3} D={s.d} k={s.k}")
        return 0

    raw = JtagTransport()
    base = args.base
    if base is None:
        control = find_control_window(raw, HOST_CANDIDATES)
        print(f"control window {control:#x}" if control else "no control window")
        base = find_wizard(raw, control=control)
        if base is None:
            print("no Clocking Wizard found. Pass --base with the window from the")
            print("block design's address editor; a wrong base writes a divider")
            print("into a status register and the clock silently never changes.")
            return 1
    print(f"wizard at {base:#x}")
    show("now ", current(raw, base), locked(raw, base))

    if args.freq is None:
        return 0

    try:
        want = solve_best(args.freq, d=args.d) if args.k is None else None
        got = set_mhz(raw, base, args.freq, d=args.d, k=args.k if args.k else want.k)
    except ClockError as exc:
        print(f"\nrefused: {exc}")
        return 1
    show("set ", got, locked(raw, base))
    print("\nevery mesh has been reset; re-initialise and re-upload before running")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
