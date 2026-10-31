/* The dispatch engine (docs/spec/dispatch-engine.md): a hardware queue of
 * control-register writes and WAITs on completion counters, issued at one a
 * cycle. Control region 0x200-0x3FF; it reads 0 on a node built without one. */
#ifndef KA_DISPATCH_DE_H
#define KA_DISPATCH_DE_H

#include <stdint.h>

#include <ka/hal/node.h>

#define KA_DE_MAGIC 0xDE01u
#define KA_DE_WAIT  32u
/* A WAIT's source past the unit counters: moves done since the clear. */
#define KA_DE_MOVER 16u
#define KA_DE_UNITS 16u
#define KA_DE_CTR_MASK 0xfffu

/* Write codes: mailbox register r, mover register byte offset o, interlink
 * register byte offset o (from KA_R_IL). */
#define KA_DE_MB(r) ((unsigned)(r))
#define KA_DE_MV(o) (8u + ((unsigned)(o) >> 3))
#define KA_DE_IL(o) (24u + ((unsigned)(o) >> 3))

#define KA_DE_STAT_MAGIC(s)  ((unsigned)((s) >> 48))
#define KA_DE_STAT_DEPTH(s)  (1u << (((s) >> 40) & 0xffu))
#define KA_DE_STAT_ISSUED(s) ((unsigned)(((s) >> 16) & 0xffffu))
#define KA_DE_STAT_USED(s)   ((unsigned)((s) & 0xffffu))
#define KA_DE_STAT_OVF(s)    (((s) >> 35) & 1u)

static inline void ka_de_queue(unsigned code, uint64_t v) { ka_ctrl_wr(KA_R_DE + 8 * code, v); }

#endif
