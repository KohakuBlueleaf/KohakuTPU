/* Compute units, the mover and the dispatch engine: generic. A unit is a
 * coordinate, a CU_TYPE and a credit from the boot table; an instruction is
 * four opaque words. Completions are counted per unit from the completion
 * queue (or mirrored from the engine's counters once the engine owns a unit),
 * CU_CTRL replies are taken from the RX queue. Nothing here knows what any
 * unit computes. */
#include "kernel.h"

#define NM(r) (HW_R_NM + 8 * (r))
#define RX(r) (HW_R_RX + 8 * (r))
#define MV(r) (HW_R_MV + (r))

struct kunit kunits[KOS_MAX_UNITS];
unsigned knunits;

/* per unit: retired = done + faults; engine-owned units mirror a counter */
static uint64_t retired[KOS_MAX_UNITS];
static int de_mapped[KOS_MAX_UNITS];
static uint32_t de_seen[KOS_MAX_UNITS];
static int de_ready;

static int find(unsigned x, unsigned y)
{
    for (unsigned i = 0; i < knunits; ++i) {
        if (kunits[i].local && kunits[i].u.x == x && kunits[i].u.y == y) {
            return (int)i;
        }
    }
    return -1;
}

static void pending_init(void);

void k_units_init(void)
{
    pending_init();
    knunits = k_boot.nunits;
    for (unsigned i = 0; i < knunits; ++i) {
        kunits[i].u = k_boot.unit[i];
        kunits[i].local = kunits[i].u.mesh == kn.mesh;
    }
    hw_wr(NM(HW_NM_DST), 0);
    k_units_poll();
}

void k_units_poll(void)
{
    uint64_t w;
    while (HW_NM_COUNT(hw_rd(NM(HW_NM_STAT)))) {
        w = hw_rd(NM(HW_NM_HEAD));
        hw_wr(NM(HW_NM_HEAD), 0);
        int i = find(HW_CQ_X(w), HW_CQ_Y(w));
        if (i < 0) {
            continue;
        }
        struct kunit *u = &kunits[i];
        switch (HW_CQ_CODE(w)) {
        case HW_SIG_INST:
        case HW_SIG_BATCH:
            ++u->done;
            ++retired[i];
            ++kstat.completions;
            break;
        case HW_SIG_FAULT_CODE:
            ++u->faults;
            ++retired[i];
            u->fault_arg = HW_CQ_ARG(w);
            ++kstat.faults;
            break;
        case HW_SIG_DATA:
            ++u->acks;
            break;
        default:
            ++u->misc;
            break;
        }
    }
    while ((w = hw_rd(RX(HW_RX_HDR))) >> 63) {
        uint64_t p2 = hw_rd(RX(HW_RX_P2)), p3 = hw_rd(RX(HW_RX_P3));
        hw_wr(RX(HW_RX_HDR), 0);
        unsigned sx = (w >> 20) & 0xf, sy = (w >> 16) & 0xf, ty = (w >> 12) & 0xf;
        int i = find(sx, sy);
        if (ty == HW_T_CU_CTRL && (p3 >> 56) == 2 && i >= 0) {
            kunits[i].ctrl_value = ((p3 & 0xffffffffffffUL) << 16) | (p2 >> 48);
            kunits[i].ctrl_ready = 1;
        }
    }
    if (de_ready) {
        for (unsigned i = 0; i < knunits; ++i) {
            if (!de_mapped[i]) {
                continue;
            }
            uint32_t c = (uint32_t)hw_rd(HW_R_DE_CTR + 8 * i) & 0xfff;
            uint32_t d = (c - de_seen[i]) & 0xfff;
            de_seen[i] = c;
            kunits[i].done += d;
            retired[i] += d;
            kstat.completions += d;
        }
    }
}

uint64_t k_unit_retired(unsigned u) { return retired[u]; }

/* One flit to unit u: `dst` adds the typed bits to its coordinate. Once the
 * dispatch engine runs, it owns the mailbox registers, so every flit goes
 * through its queue: six entries, DST, ARG0..3, GO (mailbox codes 0..5). */
static void mailbox(const struct kunit *u, uint64_t dst, const uint64_t w[4])
{
    dst |= (uint64_t)u->u.y << 8 | u->u.x;
    if (de_ready) {
        while (((hw_rd(HW_R_DE_WAIT) & 0xffff) + 6) > 64) {
        }
        hw_wr(HW_R_DE_Q + 8 * 0, dst);
        for (unsigned k = 0; k < 4; ++k) {
            hw_wr(HW_R_DE_Q + 8 * (1 + k), w[k]);
        }
        hw_wr(HW_R_DE_Q + 8 * 5, 1);
        return;
    }
    while (hw_rd(NM(HW_NM_STAT)) & HW_NM_OFFERED) {
    }
    hw_wr(NM(HW_NM_DST), dst);
    hw_wr(NM(HW_NM_ARG0), w[0]);
    hw_wr(NM(HW_NM_ARG1), w[1]);
    hw_wr(NM(HW_NM_ARG2), w[2]);
    hw_wr(NM(HW_NM_ARG3), w[3]);
    hw_wr(NM(HW_NM_GO), 1);
}

/* Hand unit i to the dispatch engine: its completions are counted by engine
 * counter i from now on. 0, or KOS_EINVAL when there is no engine. */
static int engine_own(unsigned i)
{
    if (i >= 16 || !kn.engine) {
        return KOS_EINVAL;
    }
    if (!de_ready) {
        if (!HW_DE_MAGIC(hw_rd(HW_R_DE_WAIT))) {
            return KOS_EINVAL;
        }
        hw_wr(HW_R_DE_CTL, 2);
        while (hw_rd(HW_R_DE_WAIT) & (1UL << 34)) {
        }
        hw_wr(HW_R_DE_CTL, 1);
        de_ready = 1;
    }
    if (!de_mapped[i]) {
        k_units_poll();
        hw_wr(HW_R_DE_MAP, (1UL << 31) | ((uint64_t)i << 16) | (uint64_t)kunits[i].u.y << 8 | kunits[i].u.x);
        de_seen[i] = (uint32_t)hw_rd(HW_R_DE_CTR + 8 * i) & 0xfff;
        de_mapped[i] = 1;
    }
    return KOS_OK;
}

int64_t k_dispatch_local(unsigned i, const uint64_t w[4])
{
    if (i >= knunits || !kunits[i].local) {
        return KOS_EINVAL;
    }
    if ((kn.flags & KOS_F_ENGINE) && !de_mapped[i]) {
        engine_own(i); /* no engine: the mailbox */
    }
    struct kunit *u = &kunits[i];
    uint64_t credit = u->u.depth ? u->u.depth : 16;
    while (u->sent - retired[i] >= credit) {
        k_units_poll();
    }
    mailbox(u, 0, w);
    ++kstat.dispatches;
    return (int64_t)++u->sent;
}

int k_unit_ctrl_start(unsigned i, unsigned index)
{
    if (i >= knunits || !kunits[i].local) {
        return KOS_EINVAL;
    }
    struct kunit *u = &kunits[i];
    if (u->ctrl_busy) {
        return KOS_EBUSY;
    }
    u->ctrl_busy = 1;
    u->ctrl_ready = 0;
    uint64_t w[4] = {0, 0, 0, (uint64_t)index << 48};
    mailbox(u, HW_NM_TYPED(HW_T_CU_CTRL), w);
    if (!de_ready) {
        while (hw_rd(NM(HW_NM_STAT)) & HW_NM_OFFERED) {
        }
        hw_wr(NM(HW_NM_DST), 0);
    }
    return KOS_OK;
}

/* Hand unit i to the dispatch engine (once) and queue `count` instructions;
 * the ticket of the last, as k_dispatch_local's. */
int64_t k_engine_run(unsigned i, const uint64_t *w, unsigned count)
{
    if (i >= knunits || !kunits[i].local || engine_own(i)) {
        return KOS_EINVAL;
    }
    int64_t t = 0;
    for (unsigned k = 0; k < count; ++k) {
        t = k_dispatch_local(i, &w[4 * k]);
    }
    return t;
}

/* One 1-D copy of `words` 32-byte words between physical addresses. */
int64_t k_move(uint64_t src, uint64_t dst, uint64_t words)
{
    if (!words || words > 16384 || ((src | dst) & 31)) {
        return KOS_EINVAL;
    }
    while (HW_MV_BUSY(hw_rd(HW_R_MVSTAT))) {
        k_units_poll();
    }
    uint32_t done0 = HW_MV_DONE(hw_rd(HW_R_MVSTAT));
    for (unsigned sel = 0; sel < 2; ++sel) {
        uint64_t base = sel ? dst : src;
        hw_wr(MV(0x10), (1UL << 44) | (base << 4) | sel);
        hw_wr(MV(0x18), (32UL << 20) | (words << 4) | sel);
        hw_wr(MV(0x20), 0);
        hw_wr(MV(0x28), sel);
    }
    hw_wr(MV(0x00), (1UL << 16) | (1UL << 11) | (1UL << 3));
    uint64_t s;
    while (((s = hw_rd(HW_R_MVSTAT)), HW_MV_DONE(s) == done0) && !HW_MV_FAULT(s)) {
        k_units_poll();
    }
    while (HW_MV_BUSY(hw_rd(HW_R_MVSTAT))) {
    }
    s = hw_rd(HW_R_MVSTAT);
    return HW_MV_FAULT(s) ? -(int64_t)HW_MV_FAULT(s) : KOS_OK;
}

/* ---- requests from other nodes that finish later ---- */
struct pend {
    unsigned kind, node, started;
    uint64_t req, a, b, faults;
};
static struct pend *pend; /* in private DRAM */

static void pending_init(void) { pend = k_alloc(K_PENDING * sizeof *pend); }

void k_pending_add(unsigned kind, unsigned node, uint64_t req, uint64_t a, uint64_t b)
{
    for (unsigned i = 0; i < K_PENDING; ++i) {
        if (!pend[i].kind) {
            pend[i] = (struct pend){kind, node, 0, req, a, b, a < knunits ? kunits[a].faults : 0};
            return;
        }
    }
    k_reply(node, req, KOS_EAGAIN, 0);
}

void k_pending_poll(void)
{
    for (unsigned i = 0; i < K_PENDING; ++i) {
        struct pend *p = &pend[i];
        if (!p->kind) {
            continue;
        }
        if (p->a >= knunits || !kunits[p->a].local) {
            k_reply(p->node, p->req, KOS_EINVAL, 0);
            p->kind = 0;
            continue;
        }
        struct kunit *u = &kunits[p->a];
        if (p->kind == M_UNIT_WAIT) {
            if (u->faults != p->faults) {
                k_reply(p->node, p->req, KOS_EFAULT, u->fault_arg);
                p->kind = 0;
            } else if ((int64_t)(retired[p->a] - p->b) >= 0) {
                k_reply(p->node, p->req, KOS_OK, 0);
                p->kind = 0;
            }
        } else if (p->kind == M_UNIT_CTRL) {
            if (!p->started) {
                p->started = k_unit_ctrl_start((unsigned)p->a, (unsigned)p->b) == KOS_OK;
            } else if (u->ctrl_ready) {
                u->ctrl_ready = 0;
                u->ctrl_busy = 0;
                k_reply(p->node, p->req, KOS_OK, u->ctrl_value);
                p->kind = 0;
            }
        }
    }
}
