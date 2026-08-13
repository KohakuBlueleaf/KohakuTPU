"""Ask the attached card what it is.

    python -m examples.enumerate_card

Trusts no board description. Finds the agent's control window, reads the grid,
and asks every coordinate what it is. Import a project first if you want its unit
types named -- ``import kohakutpu`` registers ``MG`` and ``VC``.
"""

import sys

from kohakuaccel.device import (
    agent_status,
    by_type,
    enumerate_mesh,
    find_control_window,
    read_agent_caps,
)
from kohakuaccel.transport.jtag import JtagTransport
from kohakuaccel.transport.rebase import Rebased
from kohakuaccel.unit import UNITS

#: Windows worth trying before giving up. A block design assigns these, and the
#: board JSONs in this repository do not agree on which.
CANDIDATES = (
    0x4_0080_0000,
    0x4_0000_0000,
    0x8000_0000,
    0x1000_0000,
    0x0,
)


def main() -> int:
    try:
        from kohakutpu import units  # noqa: F401  (registers MG and VC)
    except ImportError:
        pass

    raw = JtagTransport()
    base = find_control_window(raw, CANDIDATES)
    if base is None:
        print("no agent answered at any candidate window", file=sys.stderr)
        return 1
    print(f"control window: {base:#x}")

    card = Rebased(raw, base)
    caps = read_agent_caps(card)
    print(
        f"agent         : FLIT_WIDTH={caps.flit_width} POS_WIDTH={caps.pos_width} "
        f"grid=[{caps.grid_lo}..{caps.grid_hi}]"
    )
    print(f"status        : {agent_status(card)}")

    print(f"\nscanning x,y in {list(caps.span())}")
    endpoints = enumerate_mesh(card, caps)
    for e in endpoints:
        named = UNITS.name_of(e.type_code)
        print(
            f"  {e.coord}  {e.name!r} v{e.version}  buffers={e.buffers} "
            f"inst_depth={e.inst_depth}  registered={named!r}"
        )

    print(f"\n{len(endpoints)} endpoints")
    for name, coords in by_type(endpoints).items():
        print(f"  {name}: {len(coords)}  {list(coords)}")
    print(f"\njtag calls: {raw.calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
