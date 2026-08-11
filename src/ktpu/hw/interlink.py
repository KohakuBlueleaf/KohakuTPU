"""The MAG-to-MAG interlink, from the driver's side.

`docs/interlink/paths.md` fixes the vocabulary this module uses -- mesh, link,
switch, adapter, packet, beat, slot, kind, class, global address, doorbell --
and names the four cross-mesh paths. Two are built:

    P1  DRAM -> DRAM   `push`           the mover writes a remote address
    P4  NoC  -> NoC    `remote drain`   a CU drains into a remote CU's L1

    P2  NoC  -> DRAM   `remote store`   NOT BUILT, paths.md s6
    P3  DRAM -> NoC    `remote fill`    NOT BUILT, paths.md s6

`docs/interlink/boundary.md` is the contract this implements. The whole point of
this module is that **it works against a bitstream that has no interlink**:
:func:`caps` reads zero there and every other entry point refuses politely
rather than hanging.

That is not defensive habit, it is the shipped machine. The card in the fab is a
single mesh and always will be -- one mesh natively never needs an interlink --
so "no interlink" is the normal case and has to be the cheap one.

Two facts a caller should carry away:

* **A remote address is a wrong address on a single-mesh board.** `addr[33:32]`
  is not decoded there; it aliases into local DRAM and nothing faults. So the
  check belongs here, before the descriptor is written, and :func:`global_addr`
  refuses rather than trusting the hardware to notice.
* **A doorbell that can never ring reads exactly like one that has not rung
  yet.** :func:`wait_doorbell` therefore requires the interlink to be present
  and says so, instead of spinning on a register that is hardwired to zero.
"""

from dataclasses import dataclass

from ktpu.hw import device as dev

# ---------------------------------------------------------------------------
# The register windows. Reads come back through the agent's aux STATUS window,
# writes go out through its aux CONFIG window -- noc_orchestrator.v, and
# docs/interlink/boundary.md s3 for the layout.
# ---------------------------------------------------------------------------
A_IL_STAT = 0x0080  # + 8n, 16 words
A_IL_CFG = 0x0880  # + 8n, the top half of the aux config window

IL_CAPS = A_IL_STAT + 0x00
IL_FAULT = A_IL_STAT + 0x08
IL_DBELL0 = A_IL_STAT + 0x10  # + 8*src_mesh
IL_TX0 = A_IL_STAT + 0x30
IL_RX0 = A_IL_STAT + 0x38
IL_TX1 = A_IL_STAT + 0x40
IL_RX1 = A_IL_STAT + 0x48
IL_STALL0 = A_IL_STAT + 0x50
IL_STALL1 = A_IL_STAT + 0x58
IL_FWD = A_IL_STAT + 0x60
IL_CRED = A_IL_STAT + 0x68
IL_DOOR_TX = A_IL_STAT + 0x70

IL_CTRL = A_IL_CFG + 0x00  # [0] enable [1] clear counters [2] clear faults
IL_MESH = A_IL_CFG + 0x08  # [1:0] this mesh's id
IL_DOOR = A_IL_CFG + 0x10  # [1:0] dst mesh, [15:8] txn

IL_MAGIC = 0x494C  # "IL"

# mag_ilink.v's fault bits.
FAULTS = {
    0: "a NoC memory request carried an address outside this mesh",
    1: "a remote CU_DATA burst named no ack destination",
    2: "the switch raised a routing fault -- see mag_switch's own bits",
    3: "a write into DRAM returned an error response",
    4: "a remote flit was dropped because the interlink is disabled",
}

MESH_ADDR_SHIFT = 32
MESH_ADDR_MASK = 0x3


class NoInterlink(Exception):
    """The bitstream has no interlink, and the operation asked for one."""


@dataclass(frozen=True)
class Caps:
    """What :data:`IL_CAPS` says. Absent entirely on a single-mesh bitstream."""

    version: int
    mesh_count: int
    mesh_id: int
    nlink: int

    @classmethod
    def decode(cls, word: int) -> "Caps | None":
        if (word & 0xFFFF) != IL_MAGIC:
            return None
        return cls(
            version=(word >> 28) & 0xF,
            mesh_count=(word >> 24) & 0xF,
            mesh_id=(word >> 20) & 0xF,
            nlink=(word >> 16) & 0xF,
        )


def caps(t: dev.Transport, board) -> "Caps | None":
    """The interlink this bitstream has, or None if it has none.

    Zero is the answer on every bitstream that predates the interlink, because
    `noc_orchestrator`'s register read decode has always ended in
    ``default: reg_rd = 0``. There is no version to compare and nothing to get
    wrong about a machine older than the question.
    """
    return Caps.decode(t.read64(board.ctrl(IL_CAPS)))


def present(t: dev.Transport, board) -> bool:
    return caps(t, board) is not None


def _need(t: dev.Transport, board) -> Caps:
    c = caps(t, board)
    if c is None:
        raise NoInterlink(
            f"board {board.name!r} is talking to a bitstream with no interlink "
            "(IL_CAPS reads 0). Cross-mesh transfers are unavailable; a "
            "single-mesh machine reaches all of its memory locally."
        )
    return c


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------
def global_addr(mesh: int, byte_addr: int, mesh_count: int = 1) -> int:
    """`byte_addr` in `mesh`'s DRAM, as the global 34-bit address.

    Raises ValueError when the machine has one mesh and `mesh` is not 0. That
    is the whole reason this function exists rather than an inline shift: on a
    single-mesh bitstream the top two bits are not decoded, so a remote address
    does not fault, it ALIASES -- silently reading and writing local DRAM at the
    same offset. The check has to be here because the hardware cannot make it.
    """
    if not 0 <= mesh <= MESH_ADDR_MASK:
        raise ValueError(f"mesh id {mesh} is outside 0..3")
    if mesh >= mesh_count:
        raise ValueError(
            f"mesh {mesh} does not exist on a {mesh_count}-mesh machine. On a "
            "single-mesh bitstream this address would not fault -- address bits "
            "33:32 are undecoded there, so it would alias to local DRAM."
        )
    if byte_addr >> MESH_ADDR_SHIFT:
        raise ValueError(
            f"{byte_addr:#x} does not fit in one mesh's 4 GB; the top two bits "
            "are the mesh id"
        )
    return (mesh << MESH_ADDR_SHIFT) | byte_addr


def addr_mesh(addr: int) -> int:
    return (addr >> MESH_ADDR_SHIFT) & MESH_ADDR_MASK


def is_remote(addr: int, mesh_id: int) -> bool:
    return addr_mesh(addr) != mesh_id


# ---------------------------------------------------------------------------
# The registers
# ---------------------------------------------------------------------------
def faults(t: dev.Transport, board) -> list[str]:
    """Sticky faults, in words. Empty on a healthy link and on no link at all."""
    if not present(t, board):
        return []
    bits = t.read64(board.ctrl(IL_FAULT))
    return [why for bit, why in FAULTS.items() if bits & (1 << bit)]


def doorbells(t: dev.Transport, board) -> list[tuple[int, int]]:
    """`(count, last_txn)` per source mesh. All zero when there is no interlink."""
    _need(t, board)
    out = []
    for m in range(4):
        w = t.read64(board.ctrl(IL_DBELL0 + 8 * m))
        out.append((w & 0xFFFF_FFFF, (w >> 32) & 0xFFFF))
    return out


def ring(t: dev.Transport, board, dst_mesh: int, txn: int = 0) -> None:
    """Send a DOORBELL to `dst_mesh`.

    Ordered AFTER every remote write this mesh has already had answered: the
    link delivers in order and the far side holds a doorbell until the writes
    ahead of it have their BRESP. So the sequence is: run the mover, wait for
    ``mv_done``, then ring -- and the consumer that wakes on it is reading data
    that is in DRAM, not in a queue.
    """
    c = _need(t, board)
    if not 0 <= dst_mesh < c.mesh_count:
        raise ValueError(f"mesh {dst_mesh} is outside 0..{c.mesh_count - 1}")
    t.write64(board.ctrl(IL_DOOR), (txn & 0xFF) << 8 | (dst_mesh & 0x3))


def wait_doorbell(t: dev.Transport, board, src_mesh: int, count: int,
                  polls: int = 100_000) -> int:
    """Poll until mesh `src_mesh` has rung at least `count` times.

    Requires the interlink: on a single-mesh bitstream the counter is hardwired
    to zero, which is indistinguishable from "not yet", so this would spin
    forever. Raising is the only honest answer.
    """
    _need(t, board)
    for _ in range(polls):
        got = t.read64(board.ctrl(IL_DBELL0 + 8 * src_mesh)) & 0xFFFF_FFFF
        if got >= count:
            return got
    raise TimeoutError(
        f"mesh {src_mesh} rang {got} times, wanted {count}. Check IL_FAULT and "
        "IL_CRED -- a link out of credit reads exactly like an idle one."
    )


def counters(t: dev.Transport, board) -> dict:
    """Traffic and stall cycles per link, for a cycle budget rather than a guess.

    `stalled` is cycles a link had something to send and no credit for it, which
    is the only number that argues for a deeper credit; `idle` is cycles it had
    nothing, which argues for the opposite.
    """
    _need(t, board)
    out = {}
    for n, (tx, rx, st) in enumerate(
        ((IL_TX0, IL_RX0, IL_STALL0), (IL_TX1, IL_RX1, IL_STALL1))
    ):
        wtx, wrx, wst = (t.read64(board.ctrl(a)) for a in (tx, rx, st))
        out[f"link{n}"] = {
            "tx_packets": wtx & 0xFFFF_FFFF,
            "tx_beats": wtx >> 32,
            "rx_packets": wrx & 0xFFFF_FFFF,
            "rx_beats": wrx >> 32,
            "stalled": wst & 0xFFFF_FFFF,
            "idle": wst >> 32,
        }
    fwd = t.read64(board.ctrl(IL_FWD))
    out["forwarded"] = {"packets": fwd & 0xFFFF_FFFF, "blocked": fwd >> 32}
    out["doorbells_sent"] = t.read64(board.ctrl(IL_DOOR_TX)) & 0xFFFF_FFFF
    return out


def configure(t: dev.Transport, board, mesh_id: int | None = None,
              enable: bool = True) -> None:
    """Set this MAG's position in the grid, then enable it.

    `mesh_id` is writable so ONE bitstream can occupy any of the four positions;
    the switch derives both neighbours from it by flipping one coordinate each,
    so nothing else has to be configured and the two cannot disagree.

    Order matters: the mesh id has to be settled before traffic, because it is
    what classifies a packet as terminating or forwarded, and reclassifying
    packets already counted against a credit loses the accounting.
    """
    _need(t, board)
    t.write64(board.ctrl(IL_CTRL), 0)
    if mesh_id is not None:
        t.write64(board.ctrl(IL_MESH), mesh_id & 0x3)
    t.write64(board.ctrl(IL_CTRL), 0b111 if enable else 0)
    t.write64(board.ctrl(IL_CTRL), 1 if enable else 0)


def clear(t: dev.Transport, board) -> None:
    """Zero the counters and the sticky faults, so a measurement starts clean."""
    _need(t, board)
    t.write64(board.ctrl(IL_CTRL), 0b111)
    t.write64(board.ctrl(IL_CTRL), 0b001)


# ---------------------------------------------------------------------------
# The grid
# ---------------------------------------------------------------------------
def neighbours(mesh_id: int) -> tuple[int, int]:
    """`(x_neighbour, y_neighbour)` on the 2x2 grid, as mag_switch derives them.

    The switch computes these itself from `my_mesh`; duplicating the arithmetic
    here rather than configuring it is deliberate, because a separately
    configured peer id is a second place for the topology to be wrong.
    """
    return mesh_id ^ 1, mesh_id ^ 2


def route(src: int, dst: int) -> list[int]:
    """The meshes a packet visits, src to dst, under XY on mesh coordinates.

    X first, then Y -- the rule mag_switch implements and the reason the
    mesh-of-meshes has to stay a grid. Two hops for a diagonal, and the middle
    one is a mesh doing someone else's forwarding.
    """
    hops = [src]
    cur = src
    if (cur & 1) != (dst & 1):
        cur ^= 1
        hops.append(cur)
    if (cur & 2) != (dst & 2):
        cur ^= 2
        hops.append(cur)
    return hops
