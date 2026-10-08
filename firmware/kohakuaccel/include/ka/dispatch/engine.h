/* The dispatch engine: the mailbox driven with per-unit credit and an optional
 * node-wide completion bound. */
#ifndef KA_DISPATCH_ENGINE_H
#define KA_DISPATCH_ENGINE_H

#include <stdint.h>

#include <ka/unit/unit.h>

#define KA_MAX_PKG_UNITS 32

struct ka_unit_state {
    uint8_t x, y, mesh;
    uint16_t type;
    uint32_t credit;   /* in flight at most */
    uint32_t inflight; /* sent, not retired */
    uint32_t expected; /* completions AWAITed so far */
    uint32_t received; /* completions seen, retirements and acks alike */
    uint32_t sent;
    const struct ka_unit_class *cls;
};

struct ka_engine {
    struct ka_unit_state u[KA_MAX_PKG_UNITS];
    unsigned n;
    uint32_t cap;         /* completions the node may have outstanding */
    uint32_t outstanding; /* sum of inflight */
    uint64_t timeout;     /* cycles any one wait may take */
    uint32_t stray;       /* completions from a unit not in the package */
    uint32_t sent;
    uint64_t dst;         /* what M_DST holds, so it is written on change */
    /* the first error, kept for the completion entry */
    int status;
    unsigned detail;
    uint64_t value;
};

void ka_engine_reset(struct ka_engine *e, uint32_t cap, uint64_t timeout);

/* Add a unit from its table word and credit; returns its index, or -1. */
int ka_engine_add(struct ka_engine *e, uint64_t word, uint32_t credit);

/* Send one 256-bit payload to unit `u`, draining completions while its
 * credit or the mailbox's room is spent. 0, or a status. */
int ka_engine_send(struct ka_engine *e, unsigned u, const uint64_t w[4]);

/* Pop every completion the mailbox holds now. 0, or a status. */
int ka_engine_drain(struct ka_engine *e);

/* Wait for `count` more completions from unit `u`. */
int ka_engine_await(struct ka_engine *e, unsigned u, uint32_t count);

/* Wait until every unit has retired all it was sent and every AWAIT holds. */
int ka_engine_barrier(struct ka_engine *e);

/* After a failure: let what is in flight retire (bounded), then empty the
 * mailbox, so the next package starts with nothing of this one's queued. */
void ka_engine_quiesce(struct ka_engine *e);

/* Record the first error. Returns `status`. */
int ka_engine_fail(struct ka_engine *e, int status, unsigned detail, uint64_t value);

#endif
