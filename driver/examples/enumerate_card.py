"""Ask the attached card what it is.

    python -m examples.enumerate_card                       # trust nothing, scan
    python -m examples.enumerate_card --board multimesh_v7  # every mesh it names
    python -m examples.enumerate_card --board multimesh_v7 --port 47155

Trusts no board description. Finds the agent's control window, reads the grid,
and asks every coordinate what it is. Import a project first if you want its unit
types named -- ``import kohakutpu`` registers ``MG`` and ``VC``.

`--board` switches to the board's own windows and walks EVERY mesh; the scan is
the fallback for a bitstream no board file describes. `--port` reaches the card
through a running daemon instead of opening a second owner on the cable.
"""

import argparse
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


def show(label: str, ctrl, caps) -> None:
    """One agent's identity and every endpoint it can reach."""
    print(
        f"{label} agent : FLIT_WIDTH={caps.flit_width} POS_WIDTH={caps.pos_width} "
        f"grid=[{caps.grid_lo}..{caps.grid_hi}]  status={agent_status(ctrl)}"
    )
    endpoints = enumerate_mesh(ctrl, caps)
    for e in endpoints:
        print(
            f"    {e.coord}  {e.name!r} v{e.version}  buffers={e.buffers} "
            f"inst_depth={e.inst_depth}  registered={UNITS.name_of(e.type_code)!r}"
        )
    kinds = ", ".join(f"{k}x{len(v)}" for k, v in by_type(endpoints).items())
    print(f"    {len(endpoints)} endpoints: {kinds or 'none'}")


def from_board(name: str, port: int | None) -> int:
    """Every mesh the board names, through the daemon when one is given."""
    from kohakutpu.host import Card

    if port is None:
        transport = JtagTransport()
    else:
        from kohakuaccel.daemon.client import DaemonTransport

        transport = DaemonTransport(port=port)
    card = Card.from_board(name, transport=transport)
    print(f"{card}\n")
    for mesh in card.meshes:
        # Per mesh, not once: each reaches its DRAM through its own master, so a
        # clean path to one says nothing about the others.
        mesh.verify_write_path()
        print(f"  mesh_{mesh.index} write path clean")
        show(f"  mesh_{mesh.index}", mesh.ctrl, mesh.caps)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", help="boards/<name>.json; omit to scan")
    ap.add_argument("--port", type=int, help="reach the card through this daemon")
    args = ap.parse_args()

    try:
        from kohakutpu import units  # noqa: F401  (registers MG and VC)
    except ImportError:
        pass

    if args.board:
        return from_board(args.board, args.port)

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
