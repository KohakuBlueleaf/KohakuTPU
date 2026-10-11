/* The work package, as the firmware reads it (docs/spec/package-format.md);
 * compiler/kohakuaccel/package/format.py is the host's copy. */
#ifndef KA_PACKAGE_FORMAT_H
#define KA_PACKAGE_FORMAT_H

#include <stdint.h>

#define KA_PKG_MAGIC   0x4B50414Bu /* "KAPK" */
#define KA_PKG_VERSION 1
#define KA_PKG_HEADER  96 /* bytes: twelve 64-bit words */

/* Header words. */
enum {
    KA_PH_MAGIC    = 0, /* [31:0] magic, [47:32] version, [63:48] header bytes */
    KA_PH_SIG      = 1, /* machine signature, 0 = any machine */
    KA_PH_SIZE     = 2, /* [31:0] total bytes, [63:32] flags */
    KA_PH_COUNTS   = 3, /* [31:0] payloads, [63:32] relocations */
    KA_PH_COUNTS2  = 4, /* [15:0] buffers, [31:16] steps, [47:32] units, [63:48] ack reserve */
    KA_PH_OFF_UNIT = 5,
    KA_PH_OFF_BUF  = 6,
    KA_PH_OFF_STEP = 7,
    KA_PH_OFF_REL  = 8,
    KA_PH_OFF_PAY  = 9,
    KA_PH_CHECK    = 10, /* FNV-1a 64 of the package with this word zero */
};

#define KA_PKG_F_CHECKSUM 1u

/* Entry sizes in bytes. */
#define KA_PKG_UNIT_BYTES 16 /* w0 type<<32|mesh<<16|y<<8|x, w1 [31:0] credit */
#define KA_PKG_BUF_BYTES  32 /* w0 name hash, w1 size, w2 default address, w3 kind|mesh<<8 */
#define KA_PKG_STEP_BYTES 16 /* w0 op|flags<<8|unit<<16|count<<32, w1 arg */
#define KA_PKG_REL_BYTES  16 /* w0 payload|bit<<32|width<<40|shift<<48|buffer<<56, w1 addend */
#define KA_PKG_PAY_BYTES  32 /* one 256-bit unit word, mailbox ARG0..ARG3 */

enum ka_step_op {
    KA_OP_END       = 0,
    KA_OP_DISPATCH  = 1, /* unit, count payloads from arg */
    KA_OP_AWAIT     = 2, /* unit, count more completions */
    KA_OP_BARRIER   = 3,
    KA_OP_MOVER     = 4, /* count payloads of (register, value) pairs from arg */
    KA_OP_RING      = 5, /* unit = destination mesh, count = tag */
    KA_OP_WAIT_BELL = 6, /* unit = source mesh, count = rings since the start */
    KA_OP_SIGNAL    = 7, /* tell the host now: count = value32, arg = value64 */
    KA_OP_SETTLE    = 8, /* hold count cycles */
    KA_OP_REPEAT    = 9, /* unit, count template payloads from arg, repeated */
    KA_OP_MWAIT     = 10, /* count = moves of this package that must be done */
    KA_OP_ENGINE    = 11, /* count dispatch-engine entries, 3 a payload, from arg */
    KA_OP_FETCH     = 12, /* unit, count words its fetch port reads from address arg */
};

/* An ENGINE entry's code byte: the engine's code (0..32) | REL, where REL adds
 * the package's payload address << 24 to the value (a fetch request's ARG3). */
#define KA_ENGINE_REL 0x40u

/* Step flags (w0 [15:8]). A MOVER step POSTED starts its moves and goes on:
 * the moves it starts are waited for by an MWAIT, a BARRIER or the end. */
#define KA_STEP_F_POSTED 1u

/* A MOVER pair whose register is this is skipped (pads an odd count). */
#define KA_MOVER_SKIP 0xFFFFFFFFFFFFFFFFUL

#endif
