"""Completion status and TIMEOUT wait names, generated from the firmware's
ka/package/status.h by software/firmware/tools/gen_status.py; do not edit.
"""

#: Completion status codes (KA_ST_*).
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
    0x24: "CONTRACT",
    0x23: "NO_REACH",
    0x30: "TIMEOUT",
    0x40: "BAD_OP",
    0x50: "NO_MEMORY",
    0x51: "NO_SLOTS",
    0x52: "BAD_FREE",
    0x53: "BAD_ARG",
    0x54: "HEAP_BUSY",
}

#: What a TIMEOUT's detail says was being waited for (KA_WAIT_*).
WAITS = {
    1: "credit",
    2: "await",
    3: "barrier",
    4: "mover",
    5: "bell",
    6: "offer",
    7: "slot",
    8: "rx",
    9: "ack",
    10: "ring",
    11: "engine",
}
