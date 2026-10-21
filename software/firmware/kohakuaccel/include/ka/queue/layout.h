/* The host<->node queue region (docs/spec/node-queue.md s3), one writer per
 * KA_Q_LINE-byte line; kohakuaccel/driver/node/queue/layout.py is the host's copy. */
#ifndef KA_QUEUE_LAYOUT_H
#define KA_QUEUE_LAYOUT_H

#define KA_Q_MAGIC   0x314555455551414BUL /* "KAQUEUE1" */
#define KA_Q_VERSION 3

/* The widest whole write any host makes (XDMA: 64 bytes; JTAG: 32). */
#define KA_Q_LINE 64

enum {
    KA_Q_INFO    = 0x000, /* host: magic, version, sq_n | cq_n << 16, so | si << 32 */
    KA_Q_FW      = 0x040, /* fw: state, abi, packages done, fatal code */
    KA_Q_SQ_TAIL = 0x080, /* host */
    KA_Q_SQ_HEAD = 0x0C0, /* fw */
    KA_Q_CQ_TAIL = 0x100, /* fw */
    KA_Q_CQ_HEAD = 0x140, /* host */
    KA_Q_SO_WR   = 0x180, /* fw: bytes produced, then bytes lost */
    KA_Q_SO_RD   = 0x1C0, /* host */
    KA_Q_SI_WR   = 0x200, /* host */
    KA_Q_SI_RD   = 0x240, /* fw */
    KA_Q_UNITS   = 0x280, /* fw: unit count, then KA_MAX_UNITS unit words from 0x2C0 */
    KA_Q_HEAPS   = 0x340, /* fw: one line per region: free, largest, live | blocks << 32, fails */
    KA_Q_SQ      = 0x440, /* sq_n entries of 64 bytes, then cq_n of 32, then stdout, stdin */
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
