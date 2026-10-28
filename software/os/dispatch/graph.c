/* A node's parts of graphs: the tasks that run here, their predecessor
 * counts, a successor index, the ready ring, per-unit pending lists and the
 * unit instructions in flight. A task is done when its work is (a unit
 * instruction retired, a copy returned, a function returned 0); its
 * successors here count down, its successors on other nodes get a DEP. */
#include "kds_int.h"

#define NONE 0xffffu

static struct kds_part *find(struct kds_node *k, uint32_t gid)
{
    for (unsigned i = 0; i < KDS_GRAPHS; ++i) {
        if (k->part[i].state != LG_FREE && k->part[i].gid == gid) {
            return &k->part[i];
        }
    }
    return 0;
}

int kds_part_open(struct kds_node *k, uint32_t gid, unsigned home, uint32_t n)
{
    struct kds_part *p = 0;
    for (unsigned i = 0; i < KDS_GRAPHS && !p; ++i) {
        p = k->part[i].state == LG_FREE ? &k->part[i] : 0;
    }
    if (!p || !n || n > KDS_MAXTASKS) {
        return p ? KOS_EINVAL : KOS_EAGAIN;
    }
    uint64_t at[6], size = 0;
    const uint64_t each[6] = {sizeof(struct kds_task), 2, 2, 8, 4 * KDS_MAXPRED, 2};
    for (unsigned i = 0; i < 6; ++i) {
        at[i] = size;
        size = KOS_ALIGN_UP(size + each[i] * (n + 1), 64);
    }
    kds_ref mem;
    int64_t st = kds_arena_alloc(k, size, KDS_SERVER, &mem);
    if (st) {
        return (int)st;
    }
    uint8_t *base = kds_arena_va(k, mem);
    memset(p, 0, sizeof *p);
    p->gid = gid;
    p->state = LG_LOADING;
    k->cpu_dirty = 1; /* this node's threads may have written what the part's units read */
    p->home = home;
    p->n = n;
    p->mem = mem;
    p->t = (struct kds_task *)(base + at[0]);
    p->remain = (uint16_t *)(base + at[1]);
    p->next = (uint16_t *)(base + at[2]);
    p->ticket = (uint64_t *)(base + at[3]);
    p->pairs = (uint32_t *)(base + at[4]);
    p->ready = (uint16_t *)(base + at[5]);
    for (unsigned u = 0; u < KOS_MAX_UNITS; ++u) {
        p->phead[u] = p->ptail[u] = NONE;
    }
    return KOS_OK;
}

static void push_ready(struct kds_part *p, unsigned i)
{
    p->ready[p->rtail] = (uint16_t)i;
    p->rtail = (p->rtail + 1) % (p->n + 1);
}

/* Shell sort of the (pred, local) pairs. */
static void sort_pairs(uint32_t *a, uint32_t n)
{
    for (uint32_t gap = n / 2; gap; gap /= 2) {
        for (uint32_t i = gap; i < n; ++i) {
            uint32_t v = a[i], j = i;
            for (; j >= gap && a[j - gap] > v; j -= gap) {
                a[j] = a[j - gap];
            }
            a[j] = v;
        }
    }
}

int kds_part_add(struct kds_node *k, uint32_t gid, const struct kds_task *t, unsigned cnt)
{
    struct kds_part *p = find(k, gid);
    if (!p || p->state != LG_LOADING || p->loaded + cnt > p->n) {
        return KOS_EINVAL;
    }
    memcpy(&p->t[p->loaded], t, cnt * sizeof *t);
    p->loaded += cnt;
    if (p->loaded < p->n) {
        return KOS_OK;
    }
    /* all here: predecessor counts, the successor index, the first ready tasks */
    for (uint32_t i = 0; i < p->n; ++i) {
        p->remain[i] = KDS_MUT == 1 ? 0 : p->t[i].npred;
        for (unsigned j = 0; j < p->t[i].npred; ++j) {
            p->pairs[p->npairs++] = (uint32_t)p->t[i].pred[j] << 16 | i;
        }
        if (!p->remain[i]) {
            push_ready(p, i);
        }
    }
    sort_pairs(p->pairs, p->npairs);
    p->state = LG_LOADED;
    return KOS_OK;
}

void kds_part_go(struct kds_node *k, uint32_t gid)
{
    struct kds_part *p = find(k, gid);
    if (p && p->state == LG_LOADED) {
        p->state = LG_RUN;
        p->t0 = kos_now();
        k->any_ready = 1;
    }
}

/* Task `id` (any node's) finished: count down its successors here. */
static void succ_done(struct kds_part *p, uint32_t id)
{
    uint32_t lo = 0, hi = p->npairs, key = id << 16;
    while (lo < hi) {
        uint32_t mid = (lo + hi) / 2;
        if (p->pairs[mid] < key) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    for (; lo < p->npairs && (p->pairs[lo] >> 16) == id; ++lo) {
        unsigned i = p->pairs[lo] & 0xffff;
        if (p->remain[i] && !--p->remain[i]) {
            push_ready(p, i);
        }
    }
}

void kds_part_dep(struct kds_node *k, uint32_t gid, const uint16_t *id, unsigned cnt)
{
    struct kds_part *p = find(k, gid);
    if (!p || p->state < LG_LOADED || p->state == LG_ENDED) {
        return;
    }
    k->hw_dirty = 1; /* a unit on another die may have written memory here */
    k->any_ready = 1;
    for (unsigned i = 0; i < cnt; ++i) {
        succ_done(p, id[i]);
    }
}

static void release_part(struct kds_node *k, struct kds_part *p)
{
    if (p->state == LG_ENDED && !p->inflight) {
        kds_arena_free(k, p->mem);
        p->state = LG_FREE;
    }
}

/* The part is over (all done, a failure, or an abort): tell the home. */
static void end_part(struct kds_node *k, struct kds_part *p, int64_t status, uint32_t failed, int64_t detail)
{
    if (p->state == LG_ENDED) {
        return;
    }
    p->status = status;
    p->failed = failed;
    p->detail = detail;
    p->state = LG_ENDED;
    p->rhead = p->rtail = 0;
    for (unsigned u = 0; u < KOS_MAX_UNITS; ++u) {
        p->phead[u] = p->ptail[u] = NONE;
    }
    if (p->home == k->me) {
        kds_home_ndone(k, k->me, p->gid, status, failed, detail, p->busy);
    } else {
        struct kds_msg m = {KM_NDONE, 0, 0, p->gid, {(uint64_t)status, failed, (uint64_t)detail, p->busy}};
        kds_dep_flush(k); /* this node's DEPs go before its report */
        kds_post(k, p->home, KDS_CHAN, &m);
    }
    release_part(k, p);
}

int kds_part_abort(struct kds_node *k, uint32_t gid)
{
    struct kds_part *p = find(k, gid);
    if (p) {
        end_part(k, p, KOS_EAGAIN, 0, 0);
    }
    return p != 0;
}

static void complete(struct kds_node *k, struct kds_part *p, unsigned i)
{
    struct kds_task *t = &p->t[i];
    ++k->st.tasks;
    succ_done(p, t->id);
    k->any_ready = 1;
    for (unsigned j = 0; j < k->n; ++j) {
        if (j != k->me && (t->succ_nodes >> j & 1) && KDS_MUT != 2) {
            kds_dep_add(k, j, p->gid, (uint16_t)t->id);
        }
    }
    if (++p->done == p->n) {
        end_part(k, p, KOS_OK, 0, 0);
    }
}

static void fail(struct kds_node *k, struct kds_part *p, unsigned i, int64_t status, int64_t detail)
{
    end_part(k, p, status, p->t[i].id, detail);
}

/* bits [lo, lo + width) of a 256-bit instruction (w[0] = bits 63..0): at
 * most two words, each one mask */
static void put_bits(uint64_t w[4], unsigned lo, unsigned width, uint64_t v)
{
    while (width && lo < 256) {
        unsigned at = lo % 64, n = 64 - at < width ? 64 - at : width;
        uint64_t mask = (n == 64 ? ~0UL : (1UL << n) - 1) << at;
        w[lo / 64] = (w[lo / 64] & ~mask) | ((v << at) & mask);
        v = n == 64 ? 0 : v >> n;
        lo += n;
        width -= n;
    }
}

static int cached(struct kds_node *k) { return !(k->flags & KDS_F_UNCACHED); }

/* Before a CPU task: drop lines that units wrote behind the L1. */
static void acquire(struct kds_node *k)
{
    if (k->hw_dirty && cached(k) && KDS_MUT != 7) {
        kos_acquire();
        k->cpu_dirty = 0;
    }
    k->hw_dirty = 0;
}

static int64_t run_move(struct kds_node *k, struct kds_task *t)
{
    void *src = kds_arena_va(k, t->a[0]);
    unsigned dn = KDS_REF_NODE(t->a[1]);
    uint64_t dst, flags = 0;
    if (dn == k->me) {
        dst = (uint64_t)kds_arena_va(k, t->a[1]);
    } else {
        if (dn >= k->n || KDS_REF_OFF(t->a[1]) < k->spansz[dn] - KOS_LAND_SIZE) {
            return KOS_EINVAL; /* another node's memory: only its landing zone */
        }
        dst = KDS_REF_OFF(t->a[1]) - (k->spansz[dn] - KOS_LAND_SIZE);
        flags = KOS_MV_LAND(dn);
    }
    if (!src || (dn == k->me && !dst)) {
        return KOS_EINVAL;
    }
    int64_t r = kos_move(src, dst, t->a[2], (unsigned)flags);
    k->cpu_dirty = k->hw_dirty = 0; /* SYS_MOVE wrote the L1 back before and dropped it after */
    return r;
}

/* Run what is ready: NOP, CPU and MOVE tasks at once, unit tasks onto their
 * unit's pending list. */
int kds_parts_run(struct kds_node *k)
{
    int work = 0;
    for (unsigned g = 0; g < KDS_GRAPHS; ++g) {
        struct kds_part *p = &k->part[g];
        if (p->state != LG_RUN || p->rhead == p->rtail) {
            continue;
        }
        uint64_t t0 = kos_now();
        while (p->state == LG_RUN && p->rhead != p->rtail) {
            unsigned i = p->ready[p->rhead];
            p->rhead = (p->rhead + 1) % (p->n + 1);
            struct kds_task *t = &p->t[i];
            work = 1;
            switch (t->kind) {
            case KDS_NOP:
                complete(k, p, i);
                break;
            case KDS_CPU: {
                acquire(k);
                int64_t r = ((int64_t(*)(const uint64_t *))t->a[0])(&t->a[1]);
                k->cpu_dirty = 1;
                if (r) {
                    fail(k, p, i, KOS_EFAULT, r);
                } else {
                    complete(k, p, i);
                }
                break;
            }
            case KDS_MOVE: {
                int64_t r = run_move(k, t);
                if (r) {
                    fail(k, p, i, KOS_EFAULT, r);
                } else {
                    complete(k, p, i);
                }
                break;
            }
            case KDS_UNIT:
                for (unsigned j = 0; j < t->npatch; ++j) {
                    struct kds_patch *pt = &t->patch[j];
                    uint64_t pa = KDS_MUT == 5 ? (uint64_t)kds_arena_va(k, pt->ref) : kds_resolve(k, pt->ref);
                    put_bits(t->a, pt->lo, pt->width, pa >> pt->shift);
                }
                p->next[i] = NONE;
                if (p->ptail[t->unit] == NONE) {
                    p->phead[t->unit] = (uint16_t)i;
                } else {
                    p->next[p->ptail[t->unit]] = (uint16_t)i;
                }
                p->ptail[t->unit] = (uint16_t)i;
                k->any_pending = 1;
                break;
            default:
                fail(k, p, i, KOS_EINVAL, t->kind);
                break;
            }
        }
        p->busy += kos_now() - t0;
    }
    return work;
}

/* Issue pending unit tasks within each unit's credit; consecutive ones go as
 * one SYS_ENGINE batch where the engine takes the unit. */
int kds_units_issue(struct kds_node *k)
{
    int work = 0, left = 0;
    for (unsigned g = 0; g < KDS_GRAPHS; ++g) {
        struct kds_part *p = &k->part[g];
        if (p->state != LG_RUN) {
            continue;
        }
        for (unsigned u = 0; u < k->nunits; ++u) {
            unsigned credit = k->depth[u] < KDS_INFLIGHT ? k->depth[u] : KDS_INFLIGHT;
            while (p->phead[u] != NONE && k->fcount[u] < credit) {
                if (k->cpu_dirty && cached(k) && KDS_MUT != 6) {
                    kos_release(); /* what CPU tasks wrote, in DRAM before a unit reads it */
                    k->cpu_dirty = 0;
                }
                unsigned room = credit - k->fcount[u], engine = (k->flags & KDS_F_ENGINE) && !k->noengine[u];
                unsigned max = engine ? (room < KDS_BATCH ? room : KDS_BATCH) : 1, cnt = 0;
                uint64_t w[4 * KDS_BATCH];
                uint16_t idx[KDS_BATCH];
                for (unsigned i = p->phead[u]; i != NONE && cnt < max; i = p->next[i]) {
                    memcpy(&w[4 * cnt], p->t[i].a, 32);
                    idx[cnt++] = (uint16_t)i;
                }
                int64_t last = engine ? kos_engine(u, w, cnt) : kos_dispatch(u, w);
                if (last < 0 && engine) {
                    k->noengine[u] = 1; /* no engine here, or not for this unit */
                    continue;
                }
                if (last < 0) {
                    fail(k, p, idx[0], KOS_EINVAL, last);
                    break;
                }
                for (unsigned j = 0; j < cnt; ++j) {
                    unsigned i = idx[j], slot = (k->fhead[u] + k->fcount[u]++) % KDS_INFLIGHT;
                    p->ticket[i] = (uint64_t)last - (cnt - 1 - j);
                    k->fly[u][slot].part = (uint8_t)g;
                    k->fly[u][slot].idx = (uint16_t)i;
                    ++p->inflight;
                    ++k->in_flight;
                    p->phead[u] = p->next[i];
                }
                if (p->phead[u] == NONE) {
                    p->ptail[u] = NONE;
                }
                ++k->st.unit_batches;
                work = 1;
            }
            left |= p->phead[u] != NONE;
        }
    }
    k->any_pending = left;
    return work;
}

/* How many more retirements before one changes anything: per unit, the
 * first in-flight task with successors, the last of a part whose every
 * remaining task is in flight, half the ring when tasks wait for its credit,
 * else its last; the least over the units (a retirement on any unit counts
 * toward it, so the wait can only end early). ~0 with nothing in flight. */
uint64_t kds_units_need(struct kds_node *k)
{
    uint64_t need = ~0UL;
    for (unsigned u = 0; u < k->nunits; ++u) {
        unsigned n = k->fcount[u], m = n, waiting = 0;
        if (!n) {
            continue;
        }
        for (unsigned g = 0; g < KDS_GRAPHS; ++g) {
            waiting |= k->part[g].state == LG_RUN && k->part[g].phead[u] != NONE;
        }
        unsigned last[KDS_GRAPHS] = {0, 0, 0, 0};
        for (unsigned pos = 1; pos <= n; ++pos) {
            unsigned g = k->fly[u][(k->fhead[u] + pos - 1) % KDS_INFLIGHT].part;
            unsigned i = k->fly[u][(k->fhead[u] + pos - 1) % KDS_INFLIGHT].idx;
            if (k->part[g].t[i].succ_nodes && pos < m) {
                m = pos;
            }
            last[g] = pos;
        }
        for (unsigned g = 0; g < KDS_GRAPHS; ++g) {
            struct kds_part *p = &k->part[g];
            if (last[g] && p->inflight == p->n - p->done && last[g] < m) {
                m = last[g];
            }
        }
        if (waiting && (n + 1) / 2 < m) {
            m = (n + 1) / 2;
        }
        need = m < need ? m : need;
    }
    return need;
}

/* Retire unit tasks whose tickets the counts have passed: in order per unit.
 * A fault fails the oldest task retired with it. */
void kds_units_retire(struct kds_node *k)
{
    for (unsigned u = 0; u < k->nunits; ++u) {
        uint64_t retired = k->counts[2 * u], faults = k->counts[2 * u + 1];
        uint64_t nf = faults - k->seen_faults[u];
        k->seen_faults[u] = faults;
        while (k->fcount[u]) {
            unsigned g = k->fly[u][k->fhead[u]].part, i = k->fly[u][k->fhead[u]].idx;
            struct kds_part *p = &k->part[g];
            if ((int64_t)(retired - p->ticket[i]) < 0) {
                break;
            }
            k->fhead[u] = (k->fhead[u] + 1) % KDS_INFLIGHT;
            --k->fcount[u];
            --p->inflight;
            --k->in_flight;
            k->hw_dirty = 1;
            if (p->state == LG_RUN) {
                if (nf) {
                    fail(k, p, i, KOS_EFAULT, (int64_t)faults);
                    nf = 0;
                } else {
                    complete(k, p, i);
                }
            }
            release_part(k, p);
        }
    }
}
