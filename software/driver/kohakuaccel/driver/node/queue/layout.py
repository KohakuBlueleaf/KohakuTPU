"""The queue region's layout and codes (docs/spec/node-queue.md).

The host's copy of ``software/firmware/kohakuaccel/include/ka/queue/layout.h``
and ``ka/package/status.h``. Every field has one writer and a `LINE`-byte line
of its own: the widest whole write a host makes (XDMA 64 bytes, JTAG 32), so no
host write carries another writer's field.
"""

import struct

MAGIC = 0x314555455551414B  # "KAQUEUE1"
VERSION = 3
LINE = 64

INFO = 0x000  # host: magic, version, sq_n | cq_n << 16, so | si << 32
FW = 0x040  # fw: state, abi, packages done, fatal code
SQ_TAIL = 0x080  # host
SQ_HEAD = 0x0C0  # fw
CQ_TAIL = 0x100  # fw
CQ_HEAD = 0x140  # host
SO_WR = 0x180  # fw: produced, lost
SO_RD = 0x1C0  # host
SI_WR = 0x200  # host
SI_RD = 0x240  # fw
UNITS = 0x280  # fw: unit count, then up to 16 unit words from 0x2C0
HEAPS = 0x340  # fw: one line per region: free, largest, live | blocks << 32, fails
SQ = 0x440

#: Heap regions the firmware holds (ka/os/mem.h); 0 is by convention DRAM, 1 staging.
MEM_REGIONS = 4
MEM_DRAM, MEM_STAGING = 0, 1

SQ_BYTES = 64
CQ_BYTES = 32

# Firmware states, FW word 0.
ST_NONE, ST_READY, ST_STOPPED, ST_FATAL = 0, 1, 2, 3
STATES = {ST_NONE: "none", ST_READY: "ready", ST_STOPPED: "stopped", ST_FATAL: "fatal"}

# SQ opcodes.
OP_NOP, OP_RUN, OP_STOP, OP_HEAP, OP_ALLOC, OP_FREE = 0, 1, 2, 3, 4, 5

# RUN flags (ka/package/interp.h KA_RUN_F_*). IRQ toggles the node's host
# interrupt once the completion is posted; a host that cannot take one omits it.
RUN_F_CHECKSUM, RUN_F_IRQ = 1, 2

#: Completion status codes.
STATUS = {
    0x00: "OK",
    0x01: "PROGRESS",
    0x10: "BAD_MAGIC",
    0x11: "BAD_VERSION",
    0x12: "BAD_SIGNATURE",
    0x13: "BAD_CHECKSUM",
    0x14: "BAD_STEP",
    0x15: "TOO_LARGE",
    0x16: "BAD_RELOC",
    0x17: "BAD_UNIT",
    0x18: "BAD_LAYOUT",
    0x20: "UNIT_FAULT",
    0x21: "MBOX_OVERFLOW",
    0x22: "MOVER_FAULT",
    0x23: "NO_REACH",
    0x30: "TIMEOUT",
    0x40: "BAD_OP",
    0x50: "NO_MEMORY",
    0x51: "NO_SLOTS",
    0x52: "BAD_FREE",
    0x53: "BAD_ARG",
    0x54: "HEAP_BUSY",
}
OK, PROGRESS = 0x00, 0x01
NO_MEMORY, NO_SLOTS, BAD_FREE, BAD_ARG, HEAP_BUSY = 0x50, 0x51, 0x52, 0x53, 0x54

#: What a TIMEOUT's detail says was being waited for.
WAITS = {
    1: "credit",
    2: "await",
    3: "barrier",
    4: "mover",
    5: "doorbell",
    6: "mailbox offer",
}


def line(*words: int) -> bytes:
    """Up to eight words as one whole `LINE`-byte line."""
    w = list(words) + [0] * (LINE // 8 - len(words))
    return struct.pack(f"<{LINE // 8}Q", *w)


def geometry(sq_n: int, cq_n: int, so_bytes: int, si_bytes: int) -> dict:
    """Offsets of every ring for this geometry. Raises :class:`ValueError`."""
    if not (0 < sq_n < 1 << 16 and 0 < cq_n < 1 << 16):
        raise ValueError("ring depths are 1..65535")
    if so_bytes % LINE or si_bytes % LINE or not so_bytes or not si_bytes:
        raise ValueError(f"stdio rings are whole {LINE}-byte lines")
    sq = SQ
    cq = sq + sq_n * SQ_BYTES
    # The rings start on a line: the host writes the stdin ring in whole lines.
    so = -(-(cq + cq_n * CQ_BYTES) // LINE) * LINE
    si = so + so_bytes
    return {"sq": sq, "cq": cq, "so": so, "si": si, "end": si + si_bytes}
