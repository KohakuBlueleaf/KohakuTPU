/* The host<->node queue region (docs/spec/node-queue.md s3), one writer per
 * KA_Q_LINE-byte line; kohakuaccel/driver/node/queue/layout.py is the host's copy. */
#ifndef KA_QUEUE_LAYOUT_H
#define KA_QUEUE_LAYOUT_H

#define KA_Q_MAGIC        0x314555455551414BUL /* "KAQUEUE1" */
#define KA_Q_MIN_VERSION  3
#define KA_Q_VERSION      4
#define KA_Q_LIST_VERSION 1

/* The widest whole write any host makes (XDMA: 64 bytes; JTAG: 32). */
#define KA_Q_LINE 64

/* Words of the host-written KA_Q_INFO line (byte offset = word * 8). */
enum ka_q_info {
    KA_QI_MAGIC   = 0,
    KA_QI_VERSION = 1,
    KA_QI_RINGS   = 2,
    KA_QI_STDIO   = 3,
    KA_QI_EPOCH   = 4,
};

#define KA_QI_STDIO_MASK 0xffffffffUL

/* Words of the firmware-written KA_Q_FW line (byte offset = word * 8). */
enum ka_q_fw {
    KA_QF_STATE = 0,
    KA_QF_ABI   = 1,
    KA_QF_DONE  = 2,
    KA_QF_FATAL = 3,
};

enum ka_q_off {
    KA_Q_INFO     = 0x000, /* host: magic, version, sq_n | cq_n << 16, so | si << 32, epoch */
    KA_Q_EPOCH    = 0x020, /* host: queue generation epoch (KA_Q_INFO + 8 * KA_QI_EPOCH) */
    KA_Q_FW       = 0x040, /* fw: state, abi, packages done, fatal code */
    KA_Q_LIST_ABI = 0x060, /* fw: execution-list ABI version (KA_Q_FW + KA_Q_LINE / 2) */
    KA_Q_SQ_TAIL  = 0x080, /* host */
    KA_Q_SQ_HEAD  = 0x0C0, /* fw */
    KA_Q_CQ_TAIL  = 0x100, /* fw */
    KA_Q_CQ_HEAD  = 0x140, /* host */
    KA_Q_SO_WR    = 0x180, /* fw: bytes produced, then bytes lost */
    KA_Q_SO_RD    = 0x1C0, /* host */
    KA_Q_SI_WR    = 0x200, /* host */
    KA_Q_SI_RD    = 0x240, /* fw */
    KA_Q_UNITS    = 0x280, /* fw: unit count, then KA_MAX_UNITS unit words from 0x2C0 */
    KA_Q_HEAPS    = 0x340, /* reserved ABI offset; no heap records are published */
    KA_Q_SQ       = 0x440, /* sq_n entries of 64 bytes, then cq_n of 32, then stdout,
                              stdin, then cq_n run records */
};

#define KA_Q_SQ_BYTES 64
#define KA_Q_CQ_BYTES 32

#define KA_SQ_WORDS       8
#define KA_SQ_NBIND_SHIFT 16
#define KA_SQ_OP_MASK     0xff
#define KA_CQ_STATUS_MASK 0xffff

/* Byte offsets of a KA_Q_CQ_BYTES completion entry (docs/spec/node-queue.md s3.2). */
enum ka_cq_off {
    KA_CQ_TAG    = 0,
    KA_CQ_STATUS = 8,
    KA_CQ_CYCLES = 16,
    KA_CQ_VALUE  = 24,
};

/* The run record ring (L0.NQ.056): record i is written before CQ entry i
 * (the same index modulo cq_n), in KA_Q_REC_WORD-byte words at these offsets.
 * list_index and item_tag report KA_SQ_LIST progress/failure (0 on KA_SQ_RUN);
 * epoch records Q.epoch (KA_Q_EPOCH) on every completion. */
#define KA_Q_REC_BYTES   128
#define KA_Q_REC_VERSION 1
#define KA_Q_REC_WORD    8
enum ka_q_rec {
    KA_QR_TAG        = 0,   /* the completion's tag */
    KA_QR_SO_WR      = 8,   /* stdout bytes written when it was posted */
    KA_QR_HEAD       = 16,  /* from here a RUN's timing words (KA_PT_*), else 0 */
    KA_QR_COPY       = 24,
    KA_QR_BIND       = 32,
    KA_QR_STEPS      = 40,
    KA_QR_BARRIER    = 48,
    KA_QR_LOCAL      = 56,
    KA_QR_TOTAL      = 64,
    KA_QR_FIRST      = 72,
    KA_QR_LAST       = 80,
    KA_QR_SENT       = 88,
    KA_QR_IDLE       = 96,
    KA_QR_LIST_INDEX = 104, /* KA_SQ_LIST items completed, or failing item index */
    KA_QR_ITEM_TAG   = 112, /* KA_SQ_LIST last or failing item's tag */
    KA_QR_EPOCH      = 120, /* queue generation epoch from KA_Q_EPOCH */
};

/* Firmware states, KA_Q_FW word 0. */
enum ka_q_state {
    KA_Q_ST_NONE    = 0,
    KA_Q_ST_READY   = 1,
    KA_Q_ST_STOPPED = 2,
    KA_Q_ST_FATAL   = 3,
};

/* SQ entry words: op | nbind << 16, tag, then per op (docs/spec/node-queue.md s3.1)
 *   RUN   package, bytes, bindings, timeout, flags
 *   STOP  exit value
 *   LIST  descriptor address, item count, queue epoch, list ABI version, flags (0)
 *         Header validation refuses with BAD_ARG: detail 1 on epoch != Q.epoch,
 *         detail 2 on version != KA_Q_LIST_VERSION or flags != 0 (value = epoch).
 *         Each descriptor item is a 64-byte RUN entry; an item whose opcode is not
 *         KA_SQ_RUN fails the list at that item with BAD_OP (detail = item op) and
 *         prints the per-item failure line before posting the completion.
 * Opcodes 3..5 are reserved and refuse with BAD_OP. */
enum ka_sq_op {
    KA_SQ_NOP  = 0,
    KA_SQ_RUN  = 1,
    KA_SQ_STOP = 2,
    KA_SQ_LIST = 6,
};

/* Word indices of a KA_SQ_RUN submission entry. */
enum ka_sq_run {
    KA_SQ_RUN_OP      = 0,
    KA_SQ_RUN_TAG     = 1,
    KA_SQ_RUN_PKG     = 2,
    KA_SQ_RUN_BYTES   = 3,
    KA_SQ_RUN_BIND    = 4,
    KA_SQ_RUN_TIMEOUT = 5,
    KA_SQ_RUN_FLAGS   = 6,
};

/* Word indices of a KA_SQ_LIST submission entry. */
enum ka_sq_list {
    KA_SQ_LIST_OP      = 0,
    KA_SQ_LIST_TAG     = 1,
    KA_SQ_LIST_ADDR    = 2,
    KA_SQ_LIST_COUNT   = 3,
    KA_SQ_LIST_EPOCH   = 4,
    KA_SQ_LIST_VERSION = 5,
    KA_SQ_LIST_FLAGS   = 6,
};

#endif
