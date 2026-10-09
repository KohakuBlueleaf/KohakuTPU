/* The dispatch engine: the mailbox driven with per-unit credit and an optional
 * node-wide completion bound. */
#ifndef KA_DISPATCH_ENGINE_H
#define KA_DISPATCH_ENGINE_H

#include <stdint.h>

#include <ka/hal/cpu.h>
#include <ka/hal/node.h>
#include <ka/unit/unit.h>

#define KA_MAX_PKG_UNITS 32
/* A unit's instruction queue (INST_DEPTH in the generated tops). */
#define KA_FETCH_DEPTH 512u

struct ka_unit_state {
    uint8_t x, y, mesh;
    uint16_t type;
    uint32_t credit;   /* in flight at most */
    uint32_t inflight; /* sent, not retired */
    uint32_t expected; /* completions AWAITed so far */
    uint32_t received; /* completions seen, retirements and acks alike */
    uint32_t sent;
    uint32_t fetch;    /* 1 << 16 | port y << 8 | port x, or 0: see ka_engine_fetch */
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
    uint8_t at[16][16];   /* unit index + 1 by (x, y), 0 for none */
    uint64_t t_first, t_last; /* cycle of the first and the latest GO */
    /* the first error, kept for the completion entry */
    int status;
    unsigned detail;
    uint64_t value;
};

void ka_engine_reset(struct ka_engine *e, uint32_t cap, uint64_t timeout);

/* Add a unit from its table word, credit and fetch port; returns its index, or -1. */
int ka_engine_add(struct ka_engine *e, uint64_t word, uint32_t credit, uint32_t fetch);

/* Send one 256-bit payload to unit `u`, draining completions while its
 * credit or the mailbox's room is spent. 0, or a status. */
int ka_engine_send(struct ka_engine *e, unsigned u, const uint64_t w[4]);

/* `ka_engine_send` when nothing needs waiting for: credit in hand, the last flit
 * taken, the completion queue under half full. Returns nonzero, having sent
 * nothing, when any of those fails; the caller then sends through
 * `ka_engine_send`, which waits. */
static inline int ka_engine_send_now(struct ka_engine *e, unsigned u, const uint64_t w[4])
{
    struct ka_unit_state *s = &e->u[u];
    if (s->inflight >= s->credit || e->outstanding >= e->cap) {
        return 1;
    }
    uint64_t dst = ((uint64_t)s->y << 8) | s->x;
    if (dst != e->dst) {
        ka_nm_wr(KA_NM_DST, dst);
        e->dst = dst;
    }
    ka_nm_wr(KA_NM_ARG0, w[0]);
    ka_nm_wr(KA_NM_ARG1, w[1]);
    ka_nm_wr(KA_NM_ARG2, w[2]);
    ka_nm_wr(KA_NM_ARG3, w[3]);
    uint64_t stat = ka_nm_rd(KA_NM_STAT);
    if ((stat & KA_NM_STAT_OFFERED) || KA_NM_STAT_COUNT(stat) >= KA_NM_CQ_DEPTH / 2) {
        return 1;
    }
    ka_nm_wr(KA_NM_GO, 1);
    e->t_last = ka_cycles();
    if (!e->sent) {
        e->t_first = e->t_last;
    }
    ++s->inflight;
    ++s->sent;
    ++e->outstanding;
    ++e->sent;
    return 0;
}

/* Have memory port `port` ({y,x} as y << 8 | x) stream the `n` (<= 255) words at
 * `addr` into unit `u` as instructions, counted as `n` sends. 0, or a status. */
int ka_engine_fetch(struct ka_engine *e, unsigned u, unsigned port, uint64_t addr, uint32_t n);

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
