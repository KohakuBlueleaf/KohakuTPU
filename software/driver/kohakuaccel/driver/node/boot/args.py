"""The boot block a node's firmware reads at the scratchpad's base.

Mirrors ``software/firmware/kohakuaccel/include/ka/boot/args.h`` word for word
(docs/spec/node-queue.md s2). The host writes it after the image and before
HR_BOOT; the firmware refuses to run on a block whose magic is wrong.
"""

import struct
from dataclasses import dataclass, field

MAGIC = 0x31544F4F42414B  # "KABOOT1"
VERSION = 2
MAX_UNITS = 16
#: Nine header words, then the unit table.
WORDS = 9 + MAX_UNITS
SPAD_OFFSET = 0


def unit_word(type_code: int, mesh: int, x: int, y: int) -> int:
    """One unit table entry: ``type << 32 | mesh << 16 | y << 8 | x``."""
    return (type_code & 0xFFFF) << 32 | (mesh & 0xFF) << 16 | (y & 0xFF) << 8 | x & 0xFF


@dataclass
class BootArgs:
    """What a node is told before it boots.

    `queue` is the UNIT-GLOBAL address of the node's queue region. With no
    `units`, the firmware finds its own by a CU_CTRL caps read of every
    coordinate in ``0..scan``. `cq_depth` bounds outstanding completions
    node-wide; 0 is no bound, since the mailbox holds what it cannot queue.
    """

    queue: int
    mesh: int = 0
    scan: int = 0
    timeout: int = 2_000_000
    cq_depth: int = 0
    flags: int = 0
    units: list[int] = field(default_factory=list)

    def pack(self) -> bytes:
        """The block's bytes. Raises :class:`ValueError` past MAX_UNITS."""
        if len(self.units) > MAX_UNITS:
            raise ValueError(
                f"{len(self.units)} units; the firmware's table holds {MAX_UNITS}"
            )
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
        return struct.pack(f"<{WORDS}Q", *words)
