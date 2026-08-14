"""Control-plane encodings for addon slots that live in a NoC local link.

An addon sits between a router and an endpoint and presents the same six
signals on both faces (``docs/integrate/addon-slots.md``). It has no coordinate
of its own, so it is reached by a ``CU_CTRL`` flit addressed to the endpoint
BEHIND it, whose register index falls in this module's window. The addon
consumes that flit and answers it; the endpoint never sees one.

This is framework machinery. Nothing here names a project, a unit type or an
opcode, and no new flit field is introduced -- ``CU_CTRL`` already carries an
op, an index and a 64-bit value.
"""

from kohakuaccel.backend.isa import Field, InstFormat

#: Indices 0x00-0x0F belong to the unit and are answered by ``noc_cu_base``.
#: 0xE0 up is the addon window: eight per slot, claimed by a five-bit compare.
ADDON_WINDOW_BASE = 0xE0
ADDON_WINDOW_SIZE = 8

#: Slot identities, reported in word 0. A controller enumerates a link by
#: reading it and comparing against this table.
SLOT_NONE = 0x0000
SLOT_L2_STAGING = 0x0002

#: Operations. ``noc_cu_base`` ignores the op and always reads, so READ is what
#: a legacy unit sees; WRITE is meaningful only to an addon.
OP_READ = 0
OP_WRITE = 1
OP_READ_RESPONSE = 2

#: Register offsets inside a slot's window.
REG_CAPS = 0  #: read-only: slot id, version, window bits, store depth
REG_BASE = 1  #: read/write: the 40-bit address the slot claims from
REG_CTRL = 2  #: read/write: bit 0 enables the slot
REG_STAT = 3  #: read-only: {served, forwarded-in-window}

#: A CU_CTRL payload, request and response sharing one layout. The response
#: differs only in `op`, which is why one format decodes both.
CTRL = InstFormat(
    name="cu_ctrl",
    width=256,
    fields=(
        Field("op", 8, doc="0 read, 1 write, 2 read response"),
        Field("index", 8, doc="register index; >= 0xE0 is an addon window"),
        Field("value", 64, default=0),
    ),
    doc="CU_CTRL payload. The header carries the destination and the txn id.",
)


def slot_index(reg: int, base: int = ADDON_WINDOW_BASE) -> int:
    """The CU_CTRL index for register `reg` of the slot at `base`.

    Raises ValueError if `reg` is outside the slot's eight registers or `base`
    is not 8-aligned, either of which would silently address a different slot.
    """
    if base % ADDON_WINDOW_SIZE:
        raise ValueError(
            f"addon window base {base:#x} is not {ADDON_WINDOW_SIZE}-aligned, so "
            f"the hardware's five-bit compare would claim a different window"
        )
    if not 0 <= reg < ADDON_WINDOW_SIZE:
        raise ValueError(
            f"register {reg} is outside the slot's {ADDON_WINDOW_SIZE} registers"
        )
    return base + reg


def read(reg: int, base: int = ADDON_WINDOW_BASE) -> int:
    """The CU_CTRL payload that reads register `reg` of an addon slot."""
    return CTRL.encode(op=OP_READ, index=slot_index(reg, base), value=0)


def write(reg: int, value: int, base: int = ADDON_WINDOW_BASE) -> int:
    """The CU_CTRL payload that writes `value` to register `reg`."""
    return CTRL.encode(op=OP_WRITE, index=slot_index(reg, base), value=value)


def caps(word: int) -> dict:
    """Decode a CAPS read response into slot id, version, window bits, depth."""
    return {
        "slot": (word >> 48) & 0xFFFF,
        "version": (word >> 40) & 0xFF,
        "bits": (word >> 32) & 0xFF,
        "depth": word & 0xFFFF_FFFF,
    }


def program_l2(base_addr: int, depth: int, bits: int) -> list[int]:
    """The payloads that point an L2 staging slot at `base_addr` and enable it.

    `depth` and `bits` are what CAPS reports; they are checked here rather than
    trusted, because a window wider than the store aliases silently.

    Returns the CU_CTRL payloads in the order they must be sent. The slot is
    disabled out of reset, so nothing it might claim is claimed until the last
    of these lands.
    """
    if bits != 5 + (depth - 1).bit_length():
        raise ValueError(
            f"a {depth}-line store of 32-byte lines spans "
            f"{5 + (depth - 1).bit_length()} address bits, but the slot reports "
            f"{bits}; the window and the store must be the same size or "
            f"addresses above the store alias onto it"
        )
    if base_addr & ((1 << bits) - 1):
        raise ValueError(
            f"base {base_addr:#x} is not aligned to the {bits}-bit window; the "
            f"hardware compares only the bits above it, so the low bits would "
            f"be ignored rather than honoured"
        )
    return [write(REG_BASE, base_addr), write(REG_CTRL, 1)]
