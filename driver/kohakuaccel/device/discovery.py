"""Asking a machine what it is, instead of being told.

Every board description in this repository disagrees with at least one other one,
and at least one disagrees with the bitstream. A framework that can enumerate a
mesh does not need any of them to be right: the agent reports the grid, and every
endpoint answers CU_CAPS through the raw-flit mailbox, which is the only path to
a node that is not a dispatch.
"""

from dataclasses import dataclass

from kohakuaccel.device.flit import ctrl_reply, ctrl_request, decode_caps
from kohakuaccel.device.registers import (
    A_CAPS,
    A_RX_FLIT0,
    A_RX_POP,
    A_RX_STATUS,
    A_STATUS,
    A_TX_FLIT0,
    A_TX_KICK,
    CTRL_READ_RESP,
    CU_CAPS,
    FLIT_WORDS,
    MAG_BASE,
)
from kohakuaccel.transport.base import MASK64, WORD_BYTES, Transport

Coord = tuple[int, int]


@dataclass(frozen=True)
class AgentCaps:
    """What the agent says the mesh is, before anything has run.

    The only register whose right answer is known in advance, which makes it the
    honest first read: a wrong `flit_width` or `pos_width` means the driver and
    the bitstream disagree about the wire, and nothing after it can be trusted.
    """

    flit_width: int
    pos_width: int
    grid_lo: int
    grid_hi: int

    @property
    def routers(self) -> list[Coord]:
        span = range(self.grid_lo, self.grid_hi + 1)
        return [(x, y) for y in span for x in span]

    def span(self) -> range:
        """Every coordinate an endpoint could sit on, router grid plus edge ring."""
        return range(max(0, self.grid_lo - 1), self.grid_hi + 2)


@dataclass(frozen=True)
class Endpoint:
    """One node that answered, and what it said."""

    coord: Coord
    type_code: int
    name: str
    version: int
    buffers: int
    inst_depth: int


def read_agent_caps(t: Transport) -> AgentCaps:
    """Read A_CAPS and unpack the mesh geometry."""
    word = t.read64(MAG_BASE + A_CAPS)
    return AgentCaps(
        flit_width=word & 0xFFFF,
        pos_width=(word >> 16) & 0xFF,
        grid_lo=(word >> 24) & 0xFF,
        grid_hi=(word >> 32) & 0xFF,
    )


def agent_status(t: Transport) -> dict:
    """A_STATUS: whether the agent is busy, errored, and whether the mesh is up."""
    word = t.read64(MAG_BASE + A_STATUS)
    return {
        "busy": word & 1,
        "error": (word >> 1) & 1,
        "mesh_ready": (word >> 2) & 1,
    }


def drain_mailbox(t: Transport, limit: int = 32) -> int:
    """Pop every reply already waiting, so a stale one cannot answer the next ask.

    Returns how many were discarded. Stops at `limit` rather than spinning on a
    receive queue whose empty flag never sets.
    """
    for popped in range(limit):
        if t.read64(MAG_BASE + A_RX_STATUS) & (1 << 16):
            return popped
        t.write64(MAG_BASE + A_RX_POP, 1)
    return limit


def control_exchange(
    t: Transport, coord: Coord, index: int = CU_CAPS, txn: int = 1
) -> dict | None:
    """One mailbox round trip to `coord`. Returns the decoded reply, or None.

    UNVALIDATED, and three things that are not an answer look like one: see
    :func:`control_read`, which is what callers want. None means the receive
    queue stayed empty.
    """
    drain_mailbox(t)
    flit = ctrl_request(dst=coord, src=(0, 0), idx=index, txn=txn)
    for w in range(FLIT_WORDS):
        t.write64(MAG_BASE + A_TX_FLIT0 + w * WORD_BYTES, (flit >> (w * 64)) & MASK64)
    t.write64(MAG_BASE + A_TX_KICK, 1)

    if t.read64(MAG_BASE + A_RX_STATUS) & (1 << 16):
        return None
    reply = 0
    for w in range(FLIT_WORDS):
        reply |= t.read64(MAG_BASE + A_RX_FLIT0 + w * WORD_BYTES) << (w * 64)
    t.write64(MAG_BASE + A_RX_POP, 1)
    return ctrl_reply(reply)


def control_write(
    t: Transport, coord: Coord, index: int, value: int, txn: int = 1
) -> int | None:
    """Set one node's control register over the mailbox; returns what it answers.

    The reply carries the register, so a caller can confirm the write landed
    rather than assume it. None means nothing answered at `coord`.
    """
    from kohakuaccel.device.flit import ctrl_write as _ctrl_write

    drain_mailbox(t)
    flit = _ctrl_write(dst=coord, src=(0, 0), idx=index, value=value, txn=txn)
    for w in range(FLIT_WORDS):
        t.write64(MAG_BASE + A_TX_FLIT0 + w * WORD_BYTES, (flit >> (w * 64)) & MASK64)
    t.write64(MAG_BASE + A_TX_KICK, 1)

    if t.read64(MAG_BASE + A_RX_STATUS) & (1 << 16):
        return None
    reply = 0
    for w in range(FLIT_WORDS):
        reply |= t.read64(MAG_BASE + A_RX_FLIT0 + w * WORD_BYTES) << (w * 64)
    t.write64(MAG_BASE + A_RX_POP, 1)
    got = ctrl_reply(reply)
    return got["value"] if got["src"] == coord and got["idx"] == index else None


def control_read(t: Transport, coord: Coord, index: int = CU_CAPS, txn: int = 1):
    """Ask one node for a control register over the mailbox.

    Returns the 64-bit value, or None when NO UNIT LIVES AT `coord`. Three things
    look alike on this path and only one is an answer: an empty queue, our own
    flit looped back, and a reply from a unit that is not the one we asked.
    """
    got = control_exchange(t, coord, index, txn)
    if got is None:
        return None
    # A memory-agent port returns the request itself rather than answering it, so
    # source and tag are still ours and only the opcode tells the two apart.
    if got["op"] != CTRL_READ_RESP or got["idx"] != index or got["txn"] != txn:
        return None
    # The router CLAMPS an out-of-grid address onto the nearest real unit, which
    # answers for a coordinate holding nothing. Its own `src` is the giveaway.
    if got["src"] != coord:
        return None
    return got["value"]


def confirmed_caps(t: Transport, coord: Coord, tries: int = 4):
    """CU_CAPS at `coord`, read until two reads agree. None if nothing is there.

    A read has been seen to carry the right `src` and ANOTHER endpoint's value,
    which types a unit as something it is not -- and a dispatch to a unit that
    cannot run the instruction never retires. Raises :class:`RuntimeError` if the
    reads never agree.
    """
    seen = [control_read(t, coord, CU_CAPS)]
    for _ in range(max(1, tries - 1)):
        seen.append(control_read(t, coord, CU_CAPS))
        if seen[-1] == seen[-2]:
            return seen[-1]
    raise RuntimeError(
        f"CU_CAPS at {coord} never read the same twice in {tries} tries: "
        f"{[v if v is None else hex(v) for v in seen]}"
    )


def enumerate_mesh(t: Transport, caps: AgentCaps | None = None) -> list[Endpoint]:
    """Every coordinate that answers a CU_CAPS read, in scan order.

    Scans the router grid and the edge ring around it, because endpoints sit on
    both: a local port inside the grid, or just outside it where the coordinate
    clamp reaches them. That same clamp makes the ring answer for coordinates
    that hold nothing, which :func:`control_read` rejects by source.
    """
    caps = caps or read_agent_caps(t)
    found = []
    for y in caps.span():
        for x in caps.span():
            value = confirmed_caps(t, (x, y))
            if value is None:
                continue
            got = decode_caps(value)
            found.append(
                Endpoint(
                    coord=(x, y),
                    type_code=got["type"],
                    name=got["name"],
                    version=got["version"],
                    buffers=got["buffers"],
                    inst_depth=got["inst_depth"],
                )
            )
    return found


def by_type(endpoints: list[Endpoint]) -> dict[str, tuple[Coord, ...]]:
    """Endpoints grouped by unit type, which is what a MachineSpec wants."""
    out: dict[str, list[Coord]] = {}
    for e in endpoints:
        out.setdefault(e.name, []).append(e.coord)
    return {k: tuple(sorted(v)) for k, v in sorted(out.items())}


def answers_at(t: Transport, base: int, flit_width: int = 288) -> bool:
    """Whether an agent's A_CAPS is readable at `base` and says what it should.

    A_CAPS is the one register whose right answer is known before the machine has
    run, so a plausible `flit_width` is what identifies a window as an agent's.
    """
    try:
        word = t.read64(base + A_CAPS)
    except Exception:  # noqa: BLE001 -- probing an unmapped window; every
        return False  # backend reports that differently and none of it is news
    return (word & 0xFFFF) == flit_width and 0 < ((word >> 16) & 0xFF) <= 8


def find_control_window(t: Transport, candidates, flit_width: int = 288):
    """The base at which the agent answers, or None.

    A board description that disagrees with the bitstream is the normal case
    rather than the exception, so this exists to find the window by asking.
    """
    for base in candidates:
        if answers_at(t, base, flit_width):
            return base
    return None


def find_control_windows(t: Transport, windows, flit_width: int = 288) -> list[int]:
    """Every base in `windows` at which an agent answers, in order.

    A machine with several meshes gives each its own control window, and how many
    are populated is a property of the bitstream rather than of the board file.
    """
    return [base for base in windows if answers_at(t, base, flit_width)]
