/* The client side: starting and stopping the servers, building graphs,
 * running them, and the arena requests. Every request is one message to a
 * server's channel and one reply on the calling thread's own channel. */
#include "kds_int.h"

int64_t kds_server(uint64_t arg);

/* per node: the servers this node started */
static int64_t started_tid[KOS_MAX_NODES];
static unsigned started_mask;

static unsigned self(void) { return (unsigned)kos_sys(SYS_SELF, 0, 0, 0, 0).st; }

static struct kds_msg request(unsigned node, struct kds_msg m)
{
    unsigned me = self();
    m.gid = KDS_REPLY(me);
    struct kds_msg r = {0, 0, 0, 0, {(uint64_t)KOS_EINVAL, 0, 0, 0}};
    unsigned src;
    if (kds_send(node, KDS_CHAN, &m, 0) == KOS_OK) {
        kds_recv(KDS_REPLY(me), &r, 0, &src);
    }
    return r;
}

int kds_start(unsigned flags, uint64_t arena_bytes, unsigned node_mask)
{
    unsigned n = kos_nodes(), me = self(), want = 0;
    node_mask &= (1u << n) - 1;
    if (started_mask || !node_mask) {
        return KOS_EBUSY;
    }
    uint64_t arg = (flags & 0xff) | (uint64_t)node_mask << 8 | (uint64_t)me << 12 |
                   (KOS_ALIGN_UP(arena_bytes, 4096) >> 12) << 24;
    for (unsigned j = 0; j < n; ++j) {
        if (node_mask >> j & 1) {
            started_tid[j] = kos_spawn(kds_server, arg, j);
            if (started_tid[j] < 0) {
                return (int)started_tid[j];
            }
            started_mask |= 1u << j;
            ++want;
        }
    }
    int st = KOS_OK;
    for (unsigned i = 0; i < want; ++i) {
        struct kds_msg r;
        unsigned src;
        kds_recv(KDS_REPLY(me), &r, 0, &src);
        st = st ? st : (int)(int64_t)r.p[0];
    }
    return st;
}

int kds_stop(void)
{
    struct kds_msg q = {KM_QUIT, 0, 0, 0, {0, 0, 0, 0}};
    for (unsigned j = 0; j < KOS_MAX_NODES; ++j) {
        if (started_mask >> j & 1) {
            kds_send(j, KDS_CHAN, &q, 0);
        }
    }
    int bad = 0;
    for (unsigned j = 0; j < KOS_MAX_NODES; ++j) {
        if (started_mask >> j & 1) {
            int64_t code = 0;
            bad += kos_join(started_tid[j], &code) || code;
        }
    }
    started_mask = 0;
    return bad ? KOS_EFAULT : KOS_OK;
}

/* ---- graphs ---- */
void kds_graph_init(struct kds_graph *g, struct kds_task *buf, unsigned cap)
{
    g->t = buf;
    g->n = 0;
    g->cap = cap < KDS_MAXTASKS ? cap : KDS_MAXTASKS;
    g->err = 0;
}

static struct kds_task *add(struct kds_graph *g, unsigned kind, unsigned node, const int *pred, unsigned npred)
{
    if (g->n >= g->cap) {
        g->err = KOS_ENOMEM;
        return 0;
    }
    if (npred > KDS_MAXPRED || node >= KOS_MAX_NODES) {
        g->err = KOS_EINVAL;
        return 0;
    }
    struct kds_task *t = &g->t[g->n];
    memset(t, 0, sizeof *t);
    t->kind = (uint8_t)kind;
    t->node = (uint8_t)node;
    t->npred = (uint8_t)npred;
    for (unsigned j = 0; j < npred; ++j) {
        if (pred[j] < 0) {
            g->err = KOS_EINVAL;
            return 0;
        }
        t->pred[j] = (uint16_t)pred[j];
    }
    t->id = g->n++;
    return t;
}

int kds_nop(struct kds_graph *g, unsigned node, const int *pred, unsigned npred)
{
    struct kds_task *t = add(g, KDS_NOP, node, pred, npred);
    return t ? (int)t->id : -1;
}

int kds_unit(struct kds_graph *g, unsigned unit, const uint64_t w[4], const int *pred, unsigned npred)
{
    struct kds_task *t = add(g, KDS_UNIT, 0, pred, npred);
    if (!t) {
        return -1;
    }
    t->unit = (uint16_t)unit;
    memcpy(t->a, w, 32);
    return (int)t->id;
}

int kds_patch(struct kds_graph *g, int task, kds_ref ref, unsigned lo, unsigned width, unsigned shift)
{
    if (task < 0 || (unsigned)task >= g->n || g->t[task].kind != KDS_UNIT || g->t[task].npatch >= KDS_MAXPATCH ||
        lo + width > 256 || width > 64 || shift > 63) {
        g->err = KOS_EINVAL;
        return -1;
    }
    struct kds_task *t = &g->t[task];
    t->patch[t->npatch++] = (struct kds_patch){ref, (uint16_t)lo, (uint8_t)width, (uint8_t)shift, 0};
    return task;
}

int kds_move(struct kds_graph *g, kds_ref src, kds_ref dst, uint64_t words, const int *pred, unsigned npred)
{
    struct kds_task *t = add(g, KDS_MOVE, KDS_REF_NODE(src), pred, npred);
    if (!t) {
        return -1;
    }
    t->a[0] = src;
    t->a[1] = dst;
    t->a[2] = words;
    return (int)t->id;
}

int kds_cpu(struct kds_graph *g, unsigned node, int64_t (*fn)(const uint64_t *args), uint64_t a0, uint64_t a1,
            uint64_t a2, const int *pred, unsigned npred)
{
    struct kds_task *t = add(g, KDS_CPU, node, pred, npred);
    if (!t) {
        return -1;
    }
    t->a[0] = (uint64_t)fn;
    t->a[1] = a0;
    t->a[2] = a1;
    t->a[3] = a2;
    return (int)t->id;
}

int64_t kds_run(struct kds_graph *g, uint64_t timeout, struct kds_result *r)
{
    struct kds_result local;
    r = r ? r : &local;
    memset(r, 0, sizeof *r);
    if (g->err) {
        return r->status = g->err;
    }
    if (!kds_k) {
        return r->status = KOS_ENOENT; /* the home is this node's server */
    }
    struct kds_msg m = {KM_SUBMIT, 0, 0, 0, {(uint64_t)g->t, g->n, timeout, 0}};
    struct kds_msg a = request(kds_k->me, m);
    r->status = (int64_t)a.p[0];
    r->failed = a.p[1];
    r->detail = (int64_t)a.p[2];
    r->cycles = a.p[3];
    return r->status;
}

/* ---- memory ---- */
int64_t kds_alloc(unsigned node, uint64_t bytes, kds_ref *ref)
{
    struct kds_msg m = {KM_ALLOC, 0, 0, 0, {bytes, self(), 0, 0}};
    struct kds_msg a = request(node, m);
    *ref = a.p[1];
    return (int64_t)a.p[0];
}

int kds_free(kds_ref ref)
{
    struct kds_msg m = {KM_FREE, 0, 0, 0, {ref, 0, 0, 0}};
    return (int)(int64_t)request(KDS_REF_NODE(ref), m).p[0];
}

int64_t kds_free_owner(unsigned node)
{
    struct kds_msg m = {KM_FREE_OWNER, 0, 0, 0, {self(), 0, 0, 0}};
    return (int64_t)request(node, m).p[0];
}

int64_t kds_trim(unsigned node)
{
    struct kds_msg m = {KM_TRIM, 0, 0, 0, {0, 0, 0, 0}};
    return (int64_t)request(node, m).p[0];
}

int kds_arena_stat(unsigned node, struct kds_arena_stat *s)
{
    struct kds_msg m = {KM_STAT, 0, 0, 0, {0, 0, 0, 0}};
    struct kds_msg a = request(node, m);
    *s = (struct kds_arena_stat){a.p[0], a.p[1], a.p[2], a.p[3], a.cnt, a.flag};
    return KOS_OK;
}

kds_ref kds_land(unsigned node, uint64_t off)
{
    return KDS_REF(node, (uint64_t)kos_info_of(KOS_I_SPANSZ, node) - KOS_LAND_SIZE + off);
}

void *kds_va(kds_ref ref) { return kds_k ? kds_arena_va(kds_k, ref) : 0; }

int kds_server_stat(struct kds_server_stat *s)
{
    if (!kds_k) {
        return KOS_ENOENT;
    }
    *s = kds_k->st;
    return KOS_OK;
}

uint64_t kds_pa(kds_ref ref) { return (uint64_t)kos_info_of(KOS_I_SPAN, KDS_REF_NODE(ref)) + KDS_REF_OFF(ref); }
