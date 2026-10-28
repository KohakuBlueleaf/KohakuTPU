"""The queue region's layout and codes (docs/spec/node-queue.md).

The host's copy of ``software/firmware/kohakuaccel/include/ka/queue/layout.h``
and ``ka/package/status.h``. Every field has one writer and a `LINE`-byte line
of its own: the widest whole write a host makes (XDMA 64 bytes, JTAG 32), so no
host write carries another writer's field.
"""

import struct
from dataclasses import dataclass

#: Completion status names and TIMEOUT wait names, generated from status.h.
from kohakuaccel.driver.node.queue.status import STATUS, WAITS  # noqa: F401

MAGIC = 0x314555455551414B  # "KAQUEUE1"
VERSION = 4
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
HEAPS = 0x340  # reserved ABI offset; no heap records are published
SQ = 0x440

SQ_BYTES = 64
CQ_BYTES = 32
#: One run record (L0.NQ.056); fields come from the machine schema.
REC_BYTES = 128

# Firmware states, FW word 0.
ST_NONE, ST_READY, ST_STOPPED, ST_FATAL = 0, 1, 2, 3
STATES = {ST_NONE: "none", ST_READY: "ready", ST_STOPPED: "stopped", ST_FATAL: "fatal"}

# SQ opcodes.
OP_NOP, OP_RUN, OP_STOP = 0, 1, 2

# RUN flags (ka/package/interp.h KA_RUN_F_*). IRQ toggles the node's host
# interrupt once the completion is posted; a host that cannot take one omits it.
RUN_F_CHECKSUM, RUN_F_IRQ = 1, 2
#: The debug image only: trace this run; keep it off the dispatch engine.
RUN_F_TRACE, RUN_F_NOENGINE = 4, 8

OK, PROGRESS = 0x00, 0x01
NO_MEMORY, NO_SLOTS, BAD_FREE, BAD_ARG, HEAP_BUSY = 0x50, 0x51, 0x52, 0x53, 0x54


def line(*words: int) -> bytes:
    """Up to eight words as one whole `LINE`-byte line."""
    w = list(words) + [0] * (LINE // 8 - len(words))
    return struct.pack(f"<{LINE // 8}Q", *w)


def geometry(
    sq_n: int, cq_n: int, so_bytes: int, si_bytes: int, rec_bytes: int = REC_BYTES
) -> dict:
    """Ring offsets; `rec_bytes` is 0 for a version 3 region without records."""
    if not (0 < sq_n < 1 << 16 and 0 < cq_n < 1 << 16):
        raise ValueError("ring depths are 1..65535")
    if so_bytes % LINE or si_bytes % LINE or not so_bytes or not si_bytes:
        raise ValueError(f"stdio rings are whole {LINE}-byte lines")
    sq = SQ
    cq = sq + sq_n * SQ_BYTES
    # The rings start on a line: the host writes the stdin ring in whole lines.
    so = -(-(cq + cq_n * CQ_BYTES) // LINE) * LINE
    si = so + so_bytes
    rec = si + si_bytes
    return {
        "sq": sq,
        "cq": cq,
        "so": so,
        "si": si,
        "rec": rec,
        "end": rec + cq_n * rec_bytes,
    }


#: Queue region versions the host speaks: the record ring is version 4's.
VERSIONS = (3, 4)


@dataclass(frozen=True)
class Record:
    """A run record's schema layout: field name -> (byte offset, width)."""

    version: int
    nbytes: int
    fields: dict

    @classmethod
    def from_schema(cls, rec: dict) -> "Record":
        """Decode `fw.queue.record`, refusing fields outside the entry."""
        fields = {k: (f["offset"], f["width"]) for k, f in rec["fields"].items()}
        if rec["bytes"] != REC_BYTES or any(
            o + w > REC_BYTES for o, w in fields.values()
        ):
            raise ValueError(f"a run record is {REC_BYTES} bytes: {rec}")
        return cls(rec["version"], rec["bytes"], fields)

    def decode(self, raw: bytes) -> dict:
        return {
            k: int.from_bytes(raw[o : o + w], "little")
            for k, (o, w) in self.fields.items()
        }
