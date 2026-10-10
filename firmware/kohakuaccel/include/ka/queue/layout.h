/* The host<->node queue region (docs/spec/node-queue.md s3), one writer per
 * 32-byte line; driver/kohakuaccel/node/queue/layout.py is the host's copy. */
#ifndef KA_QUEUE_LAYOUT_H
#define KA_QUEUE_LAYOUT_H

#define KA_Q_MAGIC   0x314555455551414BUL /* "KAQUEUE1" */
#define KA_Q_VERSION 2

enum {
    KA_Q_INFO    = 0x000, /* host: magic, version, sq_n | cq_n << 16, so | si << 32 */
    KA_Q_FW      = 0x020, /* fw: state, abi, packages done, fatal code */
    KA_Q_SQ_TAIL = 0x040, /* host */
    KA_Q_SQ_HEAD = 0x060, /* fw */
    KA_Q_CQ_TAIL = 0x080, /* fw */
    KA_Q_CQ_HEAD = 0x0A0, /* host */
    KA_Q_SO_WR   = 0x0C0, /* fw: bytes produced, then bytes lost */
    KA_Q_SO_RD   = 0x0E0, /* host */
    KA_Q_SI_WR   = 0x100, /* host */
    KA_Q_SI_RD   = 0x120, /* fw */
    KA_Q_UNITS   = 0x140, /* fw: unit count, then KA_MAX_UNITS unit words from 0x160 */
    KA_Q_HEAPS   = 0x1E0, /* fw: one line per region: free, largest, live | blocks << 32, fails */
    KA_Q_SQ      = 0x280, /* sq_n entries of 64 bytes, then cq_n of 32, then stdout, stdin */
};

#define KA_Q_SQ_BYTES 64
#define KA_Q_CQ_BYTES 32

/* Firmware states, KA_Q_FW word 0. */
enum { KA_Q_ST_NONE = 0, KA_Q_ST_READY = 1, KA_Q_ST_STOPPED = 2, KA_Q_ST_FATAL = 3 };

/* SQ entry words: op | nbind << 16, tag, then per op (docs/spec/node-queue.md s3.1)
 *   RUN   package, bytes, bindings, timeout, flags
 *   STOP  exit value
 *   HEAP  region, base, bytes, granule      (bytes 0 retires the region)
 *   ALLOC region, bytes, align, tag         -> completion value: the address
 *   FREE  region, address */
enum {
    KA_SQ_NOP = 0,
    KA_SQ_RUN = 1,
    KA_SQ_STOP = 2,
    KA_SQ_HEAP = 3,
    KA_SQ_ALLOC = 4,
    KA_SQ_FREE = 5,
};

#endif
