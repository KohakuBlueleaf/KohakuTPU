/* The server: one thread per node. Its loop drains the request channel,
 * runs and issues what is ready, sends what waits in the outbox, and then
 * waits for the next event (a unit retirement or a message) with one
 * SYS_EVENT, which also hands it every unit's retired count. As a graph's
 * home it validates the graph, loads each node's part (records moved into
 * that node's landing-zone slice, PART, PART_ACK), starts every part (GO),
 * collects their reports (NDONE) and answers the client. */
#include "kds_int.h"

struct kds_node *kds_k;

uint64_t kds_resolve(struct kds_node *k, kds_ref ref) { return k->span[KDS_REF_NODE(ref)] + KDS_REF_OFF(ref); }

/* ---- sending ---- */
static int try_send(struct kds_out *o) { return kds_send(o->node, o->chan, &o->m, KOS_SEND_NOWAIT); }

void kds_post(struct kds_node *k, unsigned node, unsigned chan, const struct kds_msg *m)
{
    struct kds_out o = {node, chan, *m};
    ++k->st.msgs_out;
    if (!k->ocount && try_send(&o) == KOS_OK) {
        return;
    }
    if (k->ocount == KDS_OUTBOX) {
        kds_send(node, chan, m, 0); /* the outbox is full: wait for room */
        return;
    }
    k->out[(k->ohead + k->ocount++) % KDS_OUTBOX] = o;
}

/* Send what waits, in order per destination. */
static int outbox_flush(struct kds_node *k)
{
    unsigned blocked = 0, kept = 0, n = k->ocount;
    int sent = 0;
    for (unsigned i = 0; i < n; ++i) {
        struct kds_out o = k->out[(k->ohead + i) % KDS_OUTBOX];
        if (!(blocked >> o.node & 1) && try_send(&o) == KOS_OK) {
            sent = 1;
            continue;
        }
        blocked |= 1u << o.node;
        k->out[(k->ohead + kept++) % KDS_OUTBOX] = o;
    }
    k->ocount = kept;
    return sent;
}

static void reply(struct kds_node *k, unsigned node, unsigned chan, uint64_t p0, uint64_t p1, uint64_t p2, uint64_t p3)
{
    struct kds_msg m = {KM_REPLY, 0, 0, 0, {p0, p1, p2, p3}};
    kds_post(k, node, chan, &m);
}

void kds_dep_flush(struct kds_node *k)
{
    if (!k->any_deps) {
        return;
    }
    k->any_deps = 0;
    for (unsigned j = 0; j < k->n; ++j) {
        struct kds_dep *d = &k->dep[j];
        if (!d->cnt) {
            continue;
        }
        struct kds_msg m = {KM_DEP, 0, (uint16_t)d->cnt, d->gid, {0, 0, 0, 0}};
        memcpy(m.p, d->id, d->cnt * 2);
        kds_post(k, j, KDS_CHAN, &m);
        d->cnt = 0;
    }
}

void kds_dep_add(struct kds_node *k, unsigned node, uint32_t gid, uint16_t id)
{
    struct kds_dep *d = &k->dep[node];
    if (d->cnt && (d->gid != gid || d->cnt == 16)) {
        kds_dep_flush(k);
    }
    d->gid = gid;
    d->id[d->cnt++] = id;
    k->any_deps = 1;
}

/* ---- the home side ---- */
static struct kds_home *home_of(struct kds_node *k, uint32_t gid)
{
    for (unsigned i = 0; i < KDS_HOMES; ++i) {
        if (k->home[i].state != HG_FREE && k->home[i].gid == gid) {
            return &k->home[i];
        }
    }
    return 0;
}

static void answer(struct kds_node *k, struct kds_home *h)
{
    if (!h->answered) {
        h->answered = 1;
        if (!(k->flags & KDS_F_UNCACHED)) {
            kos_acquire(); /* the client shares this L1: what units wrote, read fresh */
            k->hw_dirty = k->cpu_dirty = 0;
        }
        reply(k, h->client_node, h->reply, (uint64_t)h->status, h->failed, (uint64_t)h->detail, kos_now() - h->t0);
    }
    if (h->reported == h->parts) {
        h->state = HG_FREE;
    }
}

static void start_next(struct kds_node *k);

/* Stop a graph: every part that has its records gets ABORT; the rest count
 * as reported. */
static void home_fail(struct kds_node *k, struct kds_home *h, int64_t status, uint32_t failed, int64_t detail)
{
    if (h->status == KOS_OK) {
        h->status = status;
        h->failed = failed;
        h->detail = detail;
    }
    h->state = HG_RUN; /* no more loading: waiting for the reports */
    h->reported |= h->parts & ~h->contacted;
    uint32_t to = h->contacted & ~h->reported & ~h->aborted;
    h->aborted |= to;
    for (unsigned j = 0; j < k->n; ++j) {
        if (!(to >> j & 1)) {
            continue;
        }
        if (j == k->me) {
            kds_part_abort(k, h->gid);
        } else {
            struct kds_msg m = {KM_ABORT, 0, 0, h->gid, {0, 0, 0, 0}};
            kds_post(k, j, KDS_CHAN, &m);
        }
    }
    if (k->loading >= 0 && &k->home[k->loading] == h) {
        k->loading = -1;
        start_next(k);
    }
    answer(k, h);
}

void kds_home_ndone(struct kds_node *k, unsigned node, uint32_t gid, int64_t status, uint32_t failed, int64_t detail,
                    uint64_t busy)
{
    (void)busy;
    struct kds_home *h = home_of(k, gid);
    if (!h) {
        return;
    }
    h->reported |= 1u << node;
    if (status != KOS_OK) {
        home_fail(k, h, status, failed, detail);
        return;
    }
    if (h->reported == h->parts) {
        answer(k, h);
    }
}

/* The next chunk of node j's part: its records gathered in id order, moved
 * into j's landing-zone slice for this home, announced by PART. */
static int send_chunk(struct kds_node *k, struct kds_home *h, unsigned j)
{
    unsigned cnt = 0;
    while (h->scan[j] < h->n && cnt < KDS_CHUNK_TASKS) {
        if (h->t[h->scan[j]].node == j) {
            k->stage[cnt++] = h->t[h->scan[j]];
        }
        ++h->scan[j];
    }
    uint64_t off = k->me * KDS_LZ_SLICE;
    int64_t r = kos_move(k->stage, off, cnt * sizeof(struct kds_task) / 32, KOS_MV_LAND(j));
    if (r) {
        return (int)r;
    }
    struct kds_msg m = {KM_PART, h->sent[j] == 0, (uint16_t)cnt, h->gid, {h->count[j], off, 0, 0}};
    h->sent[j] += cnt;
    h->contacted |= 1u << j;
    kds_post(k, j, KDS_CHAN, &m);
    return KOS_OK;
}

static void go(struct kds_node *k, struct kds_home *h)
{
    h->state = HG_RUN;
    k->loading = -1;
    for (unsigned j = 0; j < k->n; ++j) {
        if (!(h->parts >> j & 1)) {
            continue;
        }
        if (j == k->me) {
            kds_part_go(k, h->gid);
        } else {
            struct kds_msg m = {KM_GO, 0, 0, h->gid, {0, 0, 0, 0}};
            kds_post(k, j, KDS_CHAN, &m);
        }
    }
    start_next(k);
}

static void start_load(struct kds_node *k, struct kds_home *h)
{
    k->loading = (int)(h - k->home);
    h->state = HG_LOADING;
    for (unsigned j = 0; j < k->n && h->state == HG_LOADING; ++j) {
        if (!(h->parts >> j & 1)) {
            continue;
        }
        if (j != k->me) {
            int st = send_chunk(k, h, j);
            if (st) {
                home_fail(k, h, KOS_EFAULT, 0, st);
            }
            continue;
        }
        int st = kds_part_open(k, h->gid, k->me, h->count[j]);
        h->contacted |= st ? 0 : 1u << j;
        for (uint32_t i = 0; !st && i < h->n; ++i) {
            st = h->t[i].node == j ? kds_part_add(k, h->gid, &h->t[i], 1) : KOS_OK;
        }
        if (st) {
            home_fail(k, h, st, 0, 0);
        } else {
            h->acked |= 1u << j;
        }
    }
    if (h->state == HG_LOADING && h->acked == h->parts) {
        go(k, h);
    }
}

static void start_next(struct kds_node *k)
{
    if (k->loading >= 0) {
        return;
    }
    struct kds_home *next = 0;
    for (unsigned i = 0; i < KDS_HOMES; ++i) {
        struct kds_home *h = &k->home[i];
        if (h->state == HG_QUEUED && (!next || (int32_t)(h->gid - next->gid) < 0)) {
            next = h;
        }
    }
    if (next) {
        start_load(k, next);
    }
}

static void part_ack(struct kds_node *k, unsigned j, uint32_t gid)
{
    struct kds_home *h = home_of(k, gid);
    if (!h || h->state != HG_LOADING) {
        return;
    }
    if (h->sent[j] < h->count[j]) {
        int st = send_chunk(k, h, j);
        if (st) {
            home_fail(k, h, KOS_EFAULT, 0, st);
        }
        return;
    }
    h->acked |= 1u << j;
    if (h->acked == h->parts) {
        go(k, h);
    }
}

/* Check a graph, name each task's node and the nodes its successors run
 * on. 0, or a status with the bad task in *bad. */
static int64_t check(struct kds_node *k, struct kds_task *t, uint32_t n, uint32_t *bad)
{
    if (!n || n > KDS_MAXTASKS) {
        return KOS_EINVAL;
    }
    for (uint32_t i = 0; i < n; ++i) {
        struct kds_task *x = &t[i];
        *bad = i;
        x->id = i;
        x->succ_nodes = 0;
        if (x->npred > KDS_MAXPRED || x->npatch > KDS_MAXPATCH || (x->npatch && x->kind != KDS_UNIT)) {
            return KOS_EINVAL;
        }
        for (unsigned j = 0; j < x->npred; ++j) {
            if (x->pred[j] >= i && KDS_MUT != 4) {
                return KOS_EINVAL; /* only earlier tasks: every graph is acyclic */
            }
        }
        switch (x->kind) {
        case KDS_UNIT:
            if (x->unit >= k->nunits) {
                return KOS_EINVAL;
            }
            x->node = k->unit_node[x->unit];
            break;
        case KDS_MOVE:
            x->node = (uint8_t)KDS_REF_NODE(x->a[0]);
            if (!x->a[2] || x->a[2] > 16384 || KDS_REF_NODE(x->a[1]) >= k->n) {
                return KOS_EINVAL;
            }
            break;
        case KDS_CPU:
        case KDS_NOP:
            break;
        default:
            return KOS_EINVAL;
        }
        if (x->node >= k->n) {
            return KOS_EINVAL;
        }
        if (!(k->mask >> x->node & 1)) {
            return KOS_ENOENT;
        }
    }
    for (uint32_t i = 0; i < n; ++i) {
        for (unsigned j = 0; j < t[i].npred; ++j) {
            if (t[i].pred[j] < n) {
                t[t[i].pred[j]].succ_nodes |= (uint16_t)(1u << t[i].node);
            }
        }
    }
    return KOS_OK;
}

static void submit(struct kds_node *k, unsigned src, const struct kds_msg *m)
{
    struct kds_home *h = 0;
    for (unsigned i = 0; i < KDS_HOMES && !h; ++i) {
        h = k->home[i].state == HG_FREE ? &k->home[i] : 0;
    }
    if (!h) {
        reply(k, src, m->gid, (uint64_t)KOS_EAGAIN, 0, 0, 0);
        return;
    }
    memset(h, 0, sizeof *h);
    h->t = (struct kds_task *)m->p[0];
    h->n = (uint32_t)m->p[1];
    h->client_node = src;
    h->reply = m->gid;
    h->t0 = kos_now();
    h->deadline = m->p[2] ? h->t0 + m->p[2] : 0;
    uint32_t bad = 0;
    int64_t st = src == k->me ? check(k, h->t, h->n, &bad) : KOS_EINVAL; /* the records are this node's memory */
    if (st) {
        reply(k, src, m->gid, (uint64_t)st, bad, 0, 0);
        return;
    }
    h->gid = (uint32_t)k->me << 24 | (++k->seq & 0xffffff);
    for (uint32_t i = 0; i < h->n; ++i) {
        h->parts |= 1u << h->t[i].node;
        ++h->count[h->t[i].node];
    }
    h->state = HG_QUEUED;
    start_next(k);
}

static void deadlines(struct kds_node *k, uint64_t now, uint64_t *wait)
{
    for (unsigned i = 0; i < KDS_HOMES; ++i) {
        struct kds_home *h = &k->home[i];
        if (h->state == HG_FREE || !h->deadline || h->answered) {
            continue;
        }
        if ((int64_t)(now - h->deadline) >= 0) {
            home_fail(k, h, KOS_ETIME, 0, 0);
        } else if (!*wait || h->deadline - now < *wait) {
            *wait = h->deadline - now;
        }
    }
}

/* ---- the executor side and the requests ---- */
/* A chunk of this node's part (flag: the first; p0 the part's size) from
 * the landing zone: copied out, acknowledged. */
static void part(struct kds_node *k, unsigned src, const struct kds_msg *m)
{
    int st = m->flag ? kds_part_open(k, m->gid, src, (uint32_t)m->p[0]) : KOS_OK;
    const struct kds_task *lz = (const struct kds_task *)(KOS_UV_LAND + m->p[1]);
    struct kds_task t;
    for (unsigned i = 0; !st && i < m->cnt; ++i) {
        memcpy(&t, &lz[i], sizeof t);
        st = kds_part_add(k, m->gid, &t, 1);
    }
    struct kds_msg a = {st ? KM_NDONE : KM_PART_ACK, 0, 0, m->gid, {(uint64_t)(int64_t)st, 0, 0, 0}};
    kds_post(k, src, KDS_CHAN, &a);
}

static void handle(struct kds_node *k, unsigned src, const struct kds_msg *m)
{
    ++k->st.msgs_in;
    switch (m->type) {
    case KM_SUBMIT:
        submit(k, src, m);
        break;
    case KM_PART:
        part(k, src, m);
        break;
    case KM_PART_ACK:
        part_ack(k, src, m->gid);
        break;
    case KM_GO:
        kds_part_go(k, m->gid);
        break;
    case KM_DEP:
        kds_part_dep(k, m->gid, (const uint16_t *)m->p, m->cnt);
        break;
    case KM_NDONE:
        kds_home_ndone(k, src, m->gid, (int64_t)m->p[0], (uint32_t)m->p[1], (int64_t)m->p[2], m->p[3]);
        break;
    case KM_ABORT:
        if (!kds_part_abort(k, m->gid)) {
            /* no part here (it never opened, or ended): the home still counts a report */
            struct kds_msg d = {KM_NDONE, 0, 0, m->gid, {(uint64_t)KOS_EAGAIN, 0, 0, 0}};
            kds_post(k, src, KDS_CHAN, &d);
        }
        break;
    case KM_ALLOC: {
        kds_ref ref = 0;
        int64_t st = kds_arena_alloc(k, m->p[0], (unsigned)m->p[1], &ref);
        reply(k, src, m->gid, (uint64_t)st, ref, 0, 0);
        break;
    }
    case KM_FREE:
        reply(k, src, m->gid, (uint64_t)(int64_t)kds_arena_free(k, m->p[0]), 0, 0, 0);
        break;
    case KM_FREE_OWNER:
        reply(k, src, m->gid, (uint64_t)kds_arena_free_owner(k, (unsigned)m->p[0]), 0, 0, 0);
        break;
    case KM_TRIM:
        reply(k, src, m->gid, (uint64_t)kds_arena_trim(k), 0, 0, 0);
        break;
    case KM_STAT: {
        struct kds_arena_stat s;
        kds_arena_stat_of(k, &s);
        struct kds_msg r = {KM_REPLY, (uint8_t)s.free_extents, (uint16_t)s.used_extents, 0,
                            {s.chunks, s.bytes, s.free, s.largest}};
        kds_post(k, src, m->gid, &r);
        break;
    }
    case KM_QUIT:
        k->quit = 1;
        break;
    default:
        break;
    }
}

/* ---- the thread ---- */
static int init(struct kds_node *k, unsigned flags, unsigned mask, uint64_t arena)
{
    k->me = kos_node();
    k->n = kos_nodes();
    k->flags = flags;
    k->mask = mask;
    k->loading = -1;
    int64_t nu = kos_units(k->units, KOS_MAX_UNITS);
    k->nunits = nu > 0 ? (unsigned)nu : 0;
    for (unsigned j = 0; j < k->n; ++j) {
        k->span[j] = (uint64_t)kos_info_of(KOS_I_SPAN, j);
        k->spansz[j] = (uint64_t)kos_info_of(KOS_I_SPANSZ, j);
    }
    for (unsigned u = 0; u < k->nunits; ++u) {
        k->unit_node[u] = (uint8_t)k->me;
        for (unsigned j = 0; j < k->n; ++j) {
            if ((unsigned)kos_info_of(KOS_I_MESHOF, j) == k->units[u].mesh) {
                k->unit_node[u] = (uint8_t)j;
            }
        }
        k->depth[u] = k->units[u].depth ? k->units[u].depth : 16;
    }
    int st = kds_arena_init(k, arena);
    kds_ref stage;
    if (!st) {
        st = (int)kds_arena_alloc(k, KDS_LZ_SLICE, KDS_SERVER, &stage);
    }
    k->stage = st ? 0 : kds_arena_va(k, stage);
    return st;
}

/* Every page back: the arena's chunks, then the state. */
static void teardown(struct kds_node *k, uint64_t size)
{
    for (unsigned c = 0; c < KDS_CHUNKS; ++c) {
        if (k->chunk[c].size) {
            kos_munmap((void *)k->chunk[c].va, k->chunk[c].size);
        }
    }
    kos_munmap(k, size);
}

/* arg: flags [7:0], node mask [11:8], the starter's tid [23:12], arena pages [63:24] */
int64_t kds_server(uint64_t arg)
{
    unsigned flags = arg & 0xff, mask = (arg >> 8) & 0xf, starter = (arg >> 12) & 0xfff;
    uint64_t size = KOS_ALIGN_UP(sizeof(struct kds_node), 4096);
    struct kds_node *k = kos_mmap(size);
    int st = k ? init(k, flags, mask, (arg >> 24) << 12) : KOS_ENOMEM;
    struct kds_msg ready = {KM_READY, 0, 0, 0, {(uint64_t)(int64_t)st, 0, 0, 0}};
    kds_send(KOS_TID_NODE(starter), KDS_REPLY(starter), &ready, 0);
    if (st) {
        if (k) {
            teardown(k, size);
        }
        return st;
    }
    kds_k = k;
    int queued = 0;
    uint64_t t = kos_now(), t1;
    while (!k->quit) {
        ++k->st.passes;
        /* the messages SYS_EVENT counted; then everything they made ready */
        for (struct kds_msg m; queued > 0 && !k->quit; --queued) {
            unsigned src;
            if (kds_recv(KDS_CHAN, &m, KOS_NOWAIT, &src) >= 0) {
                handle(k, src, &m);
            }
        }
        t1 = kos_now();
        k->st.cyc_msgs += t1 - t;
        if (k->any_ready) {
            kds_parts_run(k); /* runs every ready ring dry */
            k->any_ready = 0;
        }
        t = kos_now();
        k->st.cyc_run += t - t1;
        if (k->any_pending) {
            kds_units_issue(k);
        }
        t1 = kos_now();
        k->st.cyc_issue += t1 - t;
        kds_dep_flush(k);
        if (k->ocount) {
            outbox_flush(k);
        }
        t = kos_now();
        k->st.cyc_send += t - t1;
        if (k->quit) {
            break;
        }
        /* nothing is ready now: wait for a retirement or a message (or the
         * outbox's retry, or a deadline) */
        uint64_t wait = k->ocount ? KDS_RETRY : 0, now, need = k->in_flight ? kds_units_need(k) : ~0UL;
        deadlines(k, t, &wait);
        uint64_t target = need == ~0UL ? k->events + (1UL << 62) : k->events + need;
        queued = kos_event(target, wait, k->counts, k->in_flight ? k->nunits : 0, KDS_CHAN, &now);
        t1 = kos_now();
        k->st.cyc_event += t1 - t;
        if (queued >= 0) {
            k->st.waits += !queued;
            k->events = now;
            if (k->in_flight) {
                kds_units_retire(k);
            }
        }
        t = kos_now();
        k->st.cyc_retire += t - t1;
    }
    while (k->ocount) {
        outbox_flush(k);
    }
    kds_k = 0;
    teardown(k, size);
    return 0;
}
