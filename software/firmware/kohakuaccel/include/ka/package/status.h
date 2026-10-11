/* Completion status codes (docs/spec/node-queue.md s4). The host decodes these
 * in kohakuaccel/driver/node/queue/layout.py. */
#ifndef KA_PACKAGE_STATUS_H
#define KA_PACKAGE_STATUS_H

enum ka_status {
    KA_ST_OK            = 0x00,
    KA_ST_PROGRESS      = 0x01, /* a SIGNAL step, not the end of the package */
    KA_ST_BAD_MAGIC     = 0x10,
    KA_ST_BAD_VERSION   = 0x11,
    KA_ST_BAD_SIGNATURE = 0x12,
    KA_ST_BAD_CHECKSUM  = 0x13,
    KA_ST_BAD_STEP      = 0x14,
    KA_ST_TOO_LARGE     = 0x15, /* more units/buffers than the firmware holds */
    KA_ST_BAD_RELOC     = 0x16,
    KA_ST_BAD_UNIT      = 0x17,
    KA_ST_BAD_LAYOUT    = 0x18, /* a section runs past the package */
    KA_ST_UNIT_FAULT    = 0x20, /* detail: unit index; value: the signal word */
    KA_ST_MBOX_OVERFLOW = 0x21, /* reserved */
    KA_ST_MOVER_FAULT   = 0x22,
    KA_ST_NO_REACH      = 0x23, /* a MOVER register outside the mover's window */
    KA_ST_TIMEOUT       = 0x30, /* detail: what was waited on */
    KA_ST_BAD_OP        = 0x40, /* an SQ entry with an unknown opcode */
    KA_ST_NO_MEMORY     = 0x50, /* no free block of the heap holds the request */
    KA_ST_NO_SLOTS      = 0x51, /* the heap's block table is full */
    KA_ST_BAD_FREE      = 0x52, /* not the start of a used block */
    KA_ST_BAD_ARG       = 0x53, /* a size, alignment, region or geometry out of range */
    KA_ST_HEAP_BUSY     = 0x54, /* reconfiguring a heap that has live blocks */
};

/* What a TIMEOUT was waiting for. */
enum ka_wait {
    KA_WAIT_CREDIT = 1,
    KA_WAIT_AWAIT  = 2,
    KA_WAIT_BARRIER = 3,
    KA_WAIT_MOVER  = 4,
    KA_WAIT_BELL   = 5,
    KA_WAIT_OFFER  = 6,
};

#endif
