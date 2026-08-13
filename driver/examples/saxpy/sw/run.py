"""Drive the saxpy machine end to end: discover, upload, dispatch, read back.

Run it with ``python -m examples.saxpy.sw.run`` from ``driver/``. It needs no
hardware and no bitstream -- :class:`kohakuaccel.sim.SimMachine` answers on the
same registers a card does.
"""

import struct

from kohakuaccel.device import (
    CU_CAPS,
    INST_DEPTH,
    MAG_BASE,
    Program,
    ctrl_reply,
    ctrl_request,
    decode_caps,
    node_status_addr,
)
from kohakuaccel.device.registers import (
    A_RX_FLIT0,
    A_RX_POP,
    A_RX_STATUS,
    A_TX_FLIT0,
    A_TX_KICK,
    FLIT_WORDS,
)
from kohakuaccel.sim import SimMachine
from kohakuaccel.transport.base import WORD_BYTES
from kohakuaccel.unit import UNITS

from . import isa
from .machine import X_ADDR, Y_ADDR, SaxpyMachine
from .unit import SaxpyUnit

ELEM = 4  # float32
MASK = (1 << 64) - 1


def discover(t, coord) -> dict | None:
    """Ask the unit at `coord` what it is, over the raw-flit mailbox.

    Returns the decoded CU_CAPS with the registry's name for the type, or None
    if nothing answered.
    """
    flit = ctrl_request(dst=coord, src=(0, 0), idx=CU_CAPS)
    for w in range(FLIT_WORDS):
        t.write64(MAG_BASE + A_TX_FLIT0 + w * WORD_BYTES, (flit >> (w * 64)) & MASK)
    t.write64(MAG_BASE + A_TX_KICK, 1)

    if t.read64(MAG_BASE + A_RX_STATUS) & (1 << 16):
        return None
    reply = 0
    for w in range(FLIT_WORDS):
        reply |= t.read64(MAG_BASE + A_RX_FLIT0 + w * WORD_BYTES) << (w * 64)
    t.write64(MAG_BASE + A_RX_POP, 1)

    caps = decode_caps(ctrl_reply(reply)["value"])
    caps["registered"] = UNITS.name_of(caps["type"])
    return caps


def build(machine: SaxpyMachine, n: int, a: float) -> tuple[Program, list]:
    """Stage one instruction per unit and dispatch them all before waiting.

    Kicking every unit before waiting on any is what makes them overlap; a
    dispatch that polls to completion before the next one starts makes N units
    take N times as long as one.
    """
    work = machine.split(n)
    prog = Program()
    for slot, (coord, off, count) in enumerate(work):
        prog.stage_flit(
            slot,
            isa.instruction(
                count, a, X_ADDR + off * ELEM, Y_ADDR + off * ELEM, txn=slot
            ),
        )
    prog.seed_credits(INST_DEPTH)
    for slot, (coord, _, _) in enumerate(work):
        prog.kick(coord[0], coord[1], slot, 1)
    return prog, work


def main() -> int:
    n, a = 64, 2.5
    x = [float(i) for i in range(n)]
    y = [float(100 - i) for i in range(n)]

    machine = SaxpyMachine(features=frozenset({"fused_multiply_add"}))
    card = SimMachine(units={c: SaxpyUnit() for c in machine.units})

    for coord in machine.units:
        caps = discover(card, coord)
        print(f"  {coord}: {caps['registered']!r} v{caps['version']}  {caps}")

    card.upload(X_ADDR, struct.pack(f"<{n}f", *x))
    card.upload(Y_ADDR, struct.pack(f"<{n}f", *y))

    prog, work = build(machine, n, a)
    base = {c: card.read64(node_status_addr(*c)) >> 8 & 0xFFFF for c, _, _ in work}
    for coord, _, _ in work:
        prog.await_node_at(coord[0], coord[1], (base[coord] + 1) & 0xFFFF)
    prog.done(0)

    code = prog.execute(card)
    got = struct.unpack(f"<{n}f", card.download(Y_ADDR, n * ELEM))
    want = [a * xi + yi for xi, yi in zip(x, y)]

    bad = [i for i, (g, w) in enumerate(zip(got, want)) if g != w]
    print(f"  dispatched {len(work)} units, DONE={code}, {len(prog)} commands")
    print(f"  {n - len(bad)}/{n} elements correct")
    if bad:
        i = bad[0]
        print(f"  first mismatch at {i}: got {got[i]}, want {want[i]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
