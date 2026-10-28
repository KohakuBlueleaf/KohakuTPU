"""The boot block a node's firmware reads at the scratchpad's base.

Mirrors ``software/firmware/kohakuaccel/include/ka/boot/args.h`` word for word
(docs/spec/node-queue.md s2). The host writes it after the image and before
HR_BOOT; the firmware refuses to run on a block whose magic is wrong.
"""

import struct
from dataclasses import dataclass, field

MAGIC = 0x31544F4F42414B  # "KABOOT1"
VERSION = 3
MAX_UNITS = 16
#: Memory regions the host assigns (base, size, use), after the unit table.
REGIONS = 4
#: Region uses (KA_REGION_*); 0 is an empty entry.
REGION_DEBUG = 1
REGION_QUEUE = 2
#: Exit prefix and refusal codes for `ka_boot_check` and queue setup (`KA_BOOT_ERR_*`).
EXIT_BOOTARGS = 0xE100_0000_0000_0000
EXIT_MASK = 0xFF00_0000_0000_0000
ERR_MAGIC, ERR_VERSION, ERR_UNITS, ERR_REGIONS, ERR_QUEUE = 1, 2, 3, 4, 0x10
REFUSALS = {
    ERR_MAGIC: "no magic",
    ERR_VERSION: "version",
    ERR_UNITS: "units",
    ERR_REGIONS: "regions",
    ERR_QUEUE: "queue",
}
#: Nine header words, the unit table, then the region table.
WORDS = 9 + MAX_UNITS + 3 * REGIONS
SPAD_OFFSET = 0


def bound(seconds: float, sys_mhz: float) -> int:
    """A wait bound in sysnode cycles: `seconds` at the active profile's
    sysnode clock (RUL.033; the firmware counts its waits in `rdcycle`)."""
    if seconds <= 0 or sys_mhz <= 0:
        raise ValueError(f"a bound of {seconds} s at {sys_mhz} MHz")
    return int(seconds * sys_mhz * 1e6)


def idle_bound(board: dict, seconds: float) -> int:
    """:func:`bound` at a board's idle profile (its ``profiles`` and
    ``idle_profile``, boards/*.json)."""
    return bound(seconds, board["profiles"][board["idle_profile"]]["sys"])


def unit_word(type_code: int, mesh: int, x: int, y: int) -> int:
    """One unit table entry: ``type << 32 | mesh << 16 | y << 8 | x``."""
    return (type_code & 0xFFFF) << 32 | (mesh & 0xFF) << 16 | (y & 0xFF) << 8 | x & 0xFF


@dataclass
class BootArgs:
    """What a node is told before it boots.

    `queue` is the UNIT-GLOBAL address of the node's queue region. With no
    `units`, the firmware finds its own by a CU_CTRL caps read of every
    coordinate in ``0..scan``. `timeout` (keyword, no default: RUL.033) is the
    wait bound in sysnode cycles, from seconds at the active profile
    (:func:`bound`). `cq_depth` bounds outstanding completions node-wide; 0 is
    no bound, since the mailbox holds what it cannot queue.
    """

    queue: int
    mesh: int = 0
    scan: int = 0
    timeout: int = field(kw_only=True)
    cq_depth: int = 0
    flags: int = 0
    units: list[int] = field(default_factory=list)
    #: ``(base, size, use)`` per region (`REGION_*`), unit-global bytes.
    regions: list[tuple[int, int, int]] = field(default_factory=list)

    def pack(self) -> bytes:
        """The block's bytes. Raises :class:`ValueError` past MAX_UNITS or
        REGIONS."""
        if len(self.units) > MAX_UNITS:
            raise ValueError(
                f"{len(self.units)} units; the firmware's table holds {MAX_UNITS}"
            )
        if len(self.regions) > REGIONS:
            raise ValueError(
                f"{len(self.regions)} regions; the firmware's table holds {REGIONS}"
            )
        for i, (base, size, use) in enumerate(self.regions):
            if not use or size <= 0 or base < 0 or base + size >= 1 << 64:
                raise ValueError("invalid boot region extent")
            for other, length, kind in self.regions[:i]:
                if use == kind or (base < other + length and other < base + size):
                    raise ValueError("overlapping or duplicate boot regions")
        queue = self.region(REGION_QUEUE)
        if queue is not None and queue[0] != self.queue:
            raise ValueError("queue header and region disagree")
        words = [
            MAGIC,
            VERSION,
            self.queue,
            self.mesh,
            self.scan,
            self.timeout,
            self.cq_depth,
            self.flags,
            len(self.units),
        ]
        words += list(self.units) + [0] * (MAX_UNITS - len(self.units))
        for base, size, use in self.regions:
            words += [base, size, use]
        words += [0] * (3 * (REGIONS - len(self.regions)))
        return struct.pack(f"<{WORDS}Q", *words)

    def region(self, use: int) -> tuple[int, int] | None:
        """``(base, size)`` of the region of `use`, or None."""
        for base, size, u in self.regions:
            if u == use and size:
                return base, size
        return None
