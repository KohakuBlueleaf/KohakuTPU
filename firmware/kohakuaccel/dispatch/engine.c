/* The dispatch engine over rv64_noc_mbox. */
#include <ka/dispatch/engine.h>
#include <ka/hal/cpu.h>
#include <ka/hal/node.h>
#include <ka/lib/stdio.h>
#include <ka/os/task.h>
#include <ka/package/status.h>

/* Mailbox status reads before an offer wait yields to the scheduler. */
#define KA_OFFER_SPIN 64

int ka_engine_fail(struct ka_engine *e, int status, unsigned detail, uint64_t value)
{
    if (!e->status) {
        e->status = status;
        e->detail = detail;
        e->value = value;
    }
    return e->status;
}

void ka_engine_reset(struct ka_engine *e, uint32_t cap, uint64_t timeout)
{
    for (unsigned i = 0; i < e->n; ++i) {
        if (e->u[i].x < 16 && e->u[i].y < 16) {
            e->at[e->u[i].x][e->u[i].y] = 0;
        }
    }
    e->n = 0;
    e->cap = cap ? cap : 1;
    e->outstanding = 0;
    e->timeout = timeout;
    e->stray = 0;
    e->sent = 0;
    e->dst = ~0UL;
    e->status = 0;
    e->detail = 0;
    e->value = 0;
}

int ka_engine_add(struct ka_engine *e, uint64_t word, uint32_t credit, uint32_t fetch)
{
    if (e->n >= KA_MAX_PKG_UNITS) {
        return -1;
    }
    struct ka_unit_state *s = &e->u[e->n];
    s->x = (uint8_t)(word & 0xff);
    s->y = (uint8_t)((word >> 8) & 0xff);
    s->mesh = (uint8_t)((word >> 16) & 0xff);
    s->type = (uint16_t)((word >> 32) & 0xffff);
    s->cls = ka_unit_class(s->type);
    uint32_t c = credit ? credit : e->cap;
    if (s->cls->credit && s->cls->credit < c) {
        c = s->cls->credit;
    }
    if (c > e->cap) {
        c = e->cap;
    }
    s->credit = c ? c : 1;
    s->inflight = s->expected = s->received = s->sent = 0;
    s->fetch = fetch;
    if (s->x < 16 && s->y < 16 && !e->at[s->x][s->y]) {
        e->at[s->x][s->y] = (uint8_t)(e->n + 1);
    }
    return (int)e->n++;
}

static int find(struct ka_engine *e, unsigned x, unsigned y)
{
    if (x < 16 && y < 16) {
        return (int)e->at[x][y] - 1;
    }
    for (unsigned i = 0; i < e->n; ++i) {
        if (e->u[i].x == x && e->u[i].y == y) {
            return (int)i;
        }
    }
    return -1;
}

int ka_engine_drain(struct ka_engine *e)
{
    uint64_t stat = ka_nm_rd(KA_NM_STAT);

    /* Pop and count every unrequested non-signal flit. */
    while (ka_rx_rd(KA_RX_HDR) >> 63) {
        ka_rx_pop();
        ++e->stray;
    }
    uint32_t retired = 0;
    for (unsigned k = KA_NM_STAT_COUNT(stat); k; --k) {
        uint64_t w = ka_nm_rd(KA_NM_HEAD);
        ka_nm_wr(KA_NM_HEAD, 1);
        int i = find(e, KA_CQ_X(w), KA_CQ_Y(w));
        if (i < 0) {
            ++e->stray;
            if (KA_CQ_CODE(w) == KA_SIG_FAULT) {
                ka_engine_fail(e, KA_ST_UNIT_FAULT, 0xffff, w);
            }
            continue;
        }
        struct ka_unit_state *s = &e->u[i];
        unsigned code = KA_CQ_CODE(w);
        enum ka_verdict v = s->cls->classify != ka_unit_generic_classify
                                ? s->cls->classify(code, KA_CQ_ARG(w))
                            : code == KA_SIG_FAULT         ? KA_V_FAULT
                            : code == KA_SIG_DATA_RECEIVED ? KA_V_ACK
                                                           : KA_V_DONE;
        ++s->received;
        if (v != KA_V_ACK && s->inflight) {
            --s->inflight;
            ++retired;
        }
        if (v == KA_V_FAULT) {
            if (s->cls->describe) {
                s->cls->describe(KA_CQ_CODE(w), KA_CQ_ARG(w));
            }
            ka_engine_fail(e, KA_ST_UNIT_FAULT, (unsigned)i, w);
        }
    }
    e->outstanding -= retired;
    return e->status;
}

/* Drain until `done(e, arg)` holds; TIMEOUT(`what`) after e->timeout cycles. */
static int wait_until(struct ka_engine *e, int (*done)(struct ka_engine *, unsigned),
                      unsigned arg, unsigned what)
{
    uint64_t t0 = ka_cycles();
    for (;;) {
        if (ka_engine_drain(e)) {
            return e->status;
        }
        if (done(e, arg)) {
            return 0;
        }
        if (ka_cycles() - t0 > e->timeout) {
            return ka_engine_fail(e, KA_ST_TIMEOUT, what, arg);
        }
        ka_yield();
    }
}

static int has_room(struct ka_engine *e, unsigned u)
{
    return e->u[u].inflight < e->u[u].credit && e->outstanding < e->cap;
}

static int not_offered(struct ka_engine *e, unsigned u)
{
    (void)e;
    (void)u;
    return !(ka_nm_rd(KA_NM_STAT) & KA_NM_STAT_OFFERED);
}

int ka_engine_send(struct ka_engine *e, unsigned u, const uint64_t w[4])
{
    if (u >= e->n) {
        return ka_engine_fail(e, KA_ST_BAD_UNIT, u, 0);
    }
    struct ka_unit_state *s = &e->u[u];
    if (!has_room(e, u) && wait_until(e, has_room, u, KA_WAIT_CREDIT)) {
        return e->status;
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

    /* Pop completions at half depth, from the read the offer check makes anyway:
     * a full queue stalls every memory read queued behind the next one. */
    uint64_t stat = ka_nm_rd(KA_NM_STAT);
    if (KA_NM_STAT_COUNT(stat) >= KA_NM_CQ_DEPTH / 2) {
        if (ka_engine_drain(e)) {
            return e->status;
        }
        stat = ka_nm_rd(KA_NM_STAT);
    }

    /* GO only once the previous flit is taken. The link takes it within a few
     * cycles, so spin briefly: a yield is a scheduler pass and a context switch. */
    for (int spin = 0; spin < KA_OFFER_SPIN && (stat & KA_NM_STAT_OFFERED); ++spin) {
        stat = ka_nm_rd(KA_NM_STAT);
    }
    if ((stat & KA_NM_STAT_OFFERED) && wait_until(e, not_offered, u, KA_WAIT_OFFER)) {
        return e->status;
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

int ka_engine_fetch(struct ka_engine *e, unsigned u, unsigned port, uint64_t addr, uint32_t n)
{
    if (u >= e->n) {
        return ka_engine_fail(e, KA_ST_BAD_UNIT, u, 0);
    }
    struct ka_unit_state *s = &e->u[u];
    /* A word the unit has no room for would hold the port's whole response
     * stream, its own operands included: never past its instruction queue. */
    uint32_t room = s->credit < KA_FETCH_DEPTH ? s->credit : KA_FETCH_DEPTH;
    uint64_t t0 = ka_cycles();
    while (s->inflight + n > room || e->outstanding + n > e->cap) {
        if (ka_engine_drain(e)) {
            return e->status;
        }
        if (ka_cycles() - t0 > e->timeout) {
            return ka_engine_fail(e, KA_ST_TIMEOUT, KA_WAIT_CREDIT, u);
        }
    }
    /* A streamed MEM_RD_REQ to the port: STREAM|INST, `n` one-word entries,
     * peer 0 the unit (mag_mem_port flag [5]). */
    uint64_t dst = KA_NM_TYPED(KA_T_MEM_RD_REQ) | (uint64_t)port;
    if (dst != e->dst) {
        ka_nm_wr(KA_NM_DST, dst);
        e->dst = dst;
    }
    ka_nm_wr(KA_NM_ARG0, 0);
    ka_nm_wr(KA_NM_ARG1, 0);
    ka_nm_wr(KA_NM_ARG2, ((uint64_t)((s->y << 4) | s->x) << 40) | (1UL << 30));
    ka_nm_wr(KA_NM_ARG3, (addr << 24) | (0x60UL << 8) | n);
    while (ka_nm_rd(KA_NM_STAT) & KA_NM_STAT_OFFERED) {
    }
    ka_nm_wr(KA_NM_GO, 1);
    s->inflight += n;
    s->sent += n;
    e->outstanding += n;
    e->sent += n;
    return 0;
}

static int awaited(struct ka_engine *e, unsigned u)
{
    return e->u[u].received >= e->u[u].expected;
}

int ka_engine_await(struct ka_engine *e, unsigned u, uint32_t count)
{
    if (u >= e->n) {
        return ka_engine_fail(e, KA_ST_BAD_UNIT, u, 0);
    }
    e->u[u].expected += count;
    return awaited(e, u) ? 0 : wait_until(e, awaited, u, KA_WAIT_AWAIT);
}

static int all_quiet(struct ka_engine *e, unsigned unused)
{
    (void)unused;
    if (e->outstanding) {
        return 0;
    }
    for (unsigned i = 0; i < e->n; ++i) {
        if (e->u[i].received < e->u[i].expected) {
            return 0;
        }
    }
    return 1;
}

int ka_engine_barrier(struct ka_engine *e)
{
    return all_quiet(e, 0) ? ka_engine_drain(e) : wait_until(e, all_quiet, 0, KA_WAIT_BARRIER);
}

void ka_engine_quiesce(struct ka_engine *e)
{
    int status = e->status;
    unsigned detail = e->detail;
    uint64_t value = e->value;
    uint64_t t0 = ka_cycles();
    while (e->outstanding && ka_cycles() - t0 < e->timeout) {
        e->status = 0;
        ka_engine_drain(e);
    }
    while (KA_NM_STAT_COUNT(ka_nm_rd(KA_NM_STAT))) {
        ka_nm_wr(KA_NM_HEAD, 1);
    }
    e->status = status;
    e->detail = detail;
    e->value = value;
}
