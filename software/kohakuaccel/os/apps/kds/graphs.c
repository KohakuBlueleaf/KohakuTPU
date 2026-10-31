/* Dispatcher graphs on any board (CPU, mover and join tasks only):
 *   kdschain K [-flags]   a K-step chain over every node: each step a CPU task
 *                         on node s % N, its value moved into the next node's
 *                         landing zone; every step checks it got its own
 *                         predecessor's value
 *   kdsreduce E           every node sums E values of its own, moves the sum
 *                         to node 0, node 0 adds them
 *   kdsbad                graphs the server must refuse or fail: a forward
 *                         predecessor, a cycle, an unknown unit, a node with no
 *                         server, a failing CPU task here and on another node,
 *                         a deadline
 *   kdsmulti K            one client per node, each homing a K-step chain at once */
#include "kdsapp.h"

#define LCG(v, s) ((v) * 6364136223846793005UL + 1442695040888963407UL + (s))
#define SEED 0x5EEDUL

static struct kds_task *tasks(unsigned n)
{
    kds_ref r;
    return kds_alloc(kos_node(), (uint64_t)n * sizeof(struct kds_task), &r) ? 0 : kds_va(r);
}

/* ---- the chain ----
 * Step s on node s % N reads {value, tag} from its landing-zone slot (step 0:
 * the seed), checks tag == s, writes {LCG(value, s), s + 1} to its buffer;
 * a move carries that word to step s + 1's slot. */
static int64_t step(const uint64_t *a)
{
    uint64_t s = a[2] & 0xffffffff, v = SEED;
    if (s) {
        volatile uint64_t *in = kds_va(a[0]);
        if (!in || in[1] != s) {
            return 0x100 + (in ? (int64_t)in[1] : 0xff); /* not my predecessor's word */
        }
        v = in[0];
    }
    uint64_t *out = kds_va(a[1]);
    out[0] = LCG(v, s);
    out[1] = s + 1;
    return 0;
}

/* Clear this node's slots [base, base + 64 * k) of the landing zone. */
static int64_t clear(const uint64_t *a)
{
    volatile uint64_t *lz = kds_va(a[0]);
    for (uint64_t i = 0; i < a[1] * 8; ++i) {
        lz[i] = ~0UL;
    }
    return 0;
}

static uint64_t slot_off(unsigned home, uint64_t s) { return KDS_LZ_USER + home * 0x2000UL + s * 64; }

/* Build and run a K-step chain homed here; 0 when the last word is right. */
static int chain(uint64_t k, uint64_t *cycles)
{
    unsigned n = kos_nodes(), me = kos_node();
    int fails = 0;
    if (k > 127) {
        k = 127;
    }
    struct kds_task *t = tasks((unsigned)(2 * k + n + 1));
    kds_ref buf[KOS_MAX_NODES];
    CHECK(fails, t, "kdschain: no task memory");
    for (unsigned j = 0; j < n; ++j) {
        CHECK(fails, kds_alloc(j, 64, &buf[j]) == 0, "kdschain: alloc on node %u", j);
    }
    if (fails) {
        return fails;
    }
    struct kds_graph g;
    kds_graph_init(&g, t, (unsigned)(2 * k + n + 1));
    int cl[KOS_MAX_NODES];
    for (unsigned j = 0; j < n; ++j) {
        cl[j] = kds_cpu(&g, j, clear, kds_land(j, slot_off(me, 0)), k + 1, 0, 0, 0);
    }
    int prev = -1;
    for (uint64_t s = 0; s < k; ++s) {
        unsigned j = (unsigned)(s % n), nx = (unsigned)((s + 1) % n);
        int pred[2] = {cl[j], prev};
        int c = kds_cpu(&g, j, step, kds_land(j, slot_off(me, s)), buf[j], s, pred, prev < 0 ? 1 : 2);
        unsigned to = s + 1 == k ? me : nx;
        int pm[2] = {c, cl[to]};
        prev = kds_move(&g, buf[j], kds_land(to, slot_off(me, s + 1)), 1, pm, 2);
    }
    struct kds_result r;
    int64_t st = kds_run(&g, 1UL << 26, &r);
    uint64_t want = SEED;
    for (uint64_t s = 0; s < k; ++s) {
        want = LCG(want, s);
    }
    volatile uint64_t *last = kds_va(kds_land(me, slot_off(me, k)));
    CHECK(fails, st == 0 && last[0] == want && last[1] == k,
          "kdschain: home %u, %lu steps: status %ld (task %lu, detail %lx), %lx tag %lu, want %lx", me, k, st,
          r.failed, r.detail, last[0], last[1], want);
    for (unsigned j = 0; j < n; ++j) {
        kds_free(buf[j]);
    }
    *cycles = r.cycles;
    return fails;
}

static int kdschain(int argc, char **argv)
{
    uint64_t num[1] = {8};
    unsigned nn;
    unsigned flags = kds_args(argc, argv, num, 1, &nn);
    int fails = kds_start(flags, KDS_ARENA, KDS_ALL_NODES) != 0;
    uint64_t cycles = 0;
    if (!fails) {
        fails += chain(num[0], &cycles);
        REPORT("kdschain.steps", num[0]);
        REPORT("kdschain.cycles", cycles);
        REPORT("kdschain.cycles_per_step", cycles / (num[0] ? num[0] : 1));
    }
    fails += kds_stop() != 0;
    kos_printf("kdschain: %lu steps over %u nodes, %lu cycles, %s\n", num[0], kos_nodes(), cycles,
               fails ? "FAIL" : "ok");
    return fails;
}
KOS_APP("kdschain", kdschain);

/* ---- the reduction ---- */
static int64_t fill(const uint64_t *a)
{
    uint64_t *d = kds_va(a[0]);
    for (uint64_t i = 0; i < a[1]; ++i) {
        d[i] = value_at(a[2] + i);
    }
    return 0;
}

static int64_t partial(const uint64_t *a)
{
    const uint64_t *d = kds_va(a[0]);
    uint64_t s = 0;
    for (uint64_t i = 0; i < a[1]; ++i) {
        s += d[i];
    }
    uint64_t *o = kds_va(a[2]);
    o[0] = s;
    return 0;
}

static int64_t total(const uint64_t *a)
{
    volatile uint64_t *in = kds_va(a[0]);
    uint64_t s = 0;
    for (uint64_t j = 0; j < a[1]; ++j) {
        s += in[j * 8];
    }
    uint64_t *o = kds_va(a[2]);
    o[0] = s;
    return 0;
}

static int kdsreduce(int argc, char **argv)
{
    uint64_t num[1] = {256};
    unsigned nn, n = kos_nodes();
    unsigned flags = kds_args(argc, argv, num, 1, &nn);
    uint64_t e = num[0];
    int fails = kds_start(flags, KDS_ARENA, KDS_ALL_NODES) != 0;
    kds_ref data[KOS_MAX_NODES], part[KOS_MAX_NODES], res = 0;
    for (unsigned j = 0; j < n && !fails; ++j) {
        CHECK(fails, !kds_alloc(j, e * 8, &data[j]) && !kds_alloc(j, 64, &part[j]), "kdsreduce: alloc node %u", j);
    }
    CHECK(fails, !kds_alloc(kos_node(), 64, &res), "kdsreduce: alloc result");
    struct kds_task *t = fails ? 0 : tasks(3 * n + 1);
    struct kds_result r = {0, 0, 0, 0};
    if (t) {
        struct kds_graph g;
        kds_graph_init(&g, t, 3 * n + 1);
        int mv[KOS_MAX_NODES];
        for (unsigned j = 0; j < n; ++j) {
            int f = kds_cpu(&g, j, fill, data[j], e, j * e, 0, 0);
            int p = kds_cpu(&g, j, partial, data[j], e, part[j], &f, 1);
            mv[j] = kds_move(&g, part[j], kds_land(kos_node(), KDS_LZ_USER + j * 64), 1, &p, 1);
        }
        kds_cpu(&g, kos_node(), total, kds_land(kos_node(), KDS_LZ_USER), n, res, mv, n);
        int64_t st = kds_run(&g, 1UL << 26, &r);
        uint64_t want = 0;
        for (uint64_t i = 0; i < n * e; ++i) {
            want += value_at(i);
        }
        uint64_t got = *(volatile uint64_t *)kds_va(res);
        CHECK(fails, st == 0 && got == want, "kdsreduce: status %ld task %lu, sum %lu want %lu", st, r.failed, got,
              want);
        REPORT("kdsreduce.cycles", r.cycles);
    }
    fails += kds_stop() != 0;
    kos_printf("kdsreduce: %lu values on each of %u nodes, %lu cycles, %s\n", e, n, r.cycles, fails ? "FAIL" : "ok");
    return fails;
}
KOS_APP("kdsreduce", kdsreduce);

/* ---- refusals and failures ---- */
static int64_t ret_arg(const uint64_t *a) { return (int64_t)a[0]; }

static int64_t nap(const uint64_t *a)
{
    kos_sleep(a[0]);
    return 0;
}

static int expect(struct kds_graph *g, int64_t want_st, uint64_t want_task, int64_t want_detail, const char *what,
                  uint64_t timeout)
{
    struct kds_result r;
    int fails = 0;
    int64_t st = kds_run(g, timeout, &r);
    CHECK(fails, st == want_st && (want_st == KOS_ETIME || (r.failed == want_task && r.detail == want_detail)),
          "kdsbad: %s: status %ld task %lu detail %ld, want %ld task %lu detail %ld", what, st, r.failed, r.detail,
          want_st, want_task, want_detail);
    kos_printf("  kdsbad %s: status %ld task %lu detail %ld in %lu cycles\n", what, st, r.failed, r.detail, r.cycles);
    return fails;
}

static int kdsbad(int argc, char **argv)
{
    uint64_t num[1];
    unsigned nn, n = kos_nodes(), far = n - 1;
    unsigned flags = kds_args(argc, argv, num, 1, &nn);
    int fails = kds_start(flags, KDS_ARENA, KDS_ALL_NODES) != 0;
    kds_ref tr;
    fails += kds_alloc(0, 8 * sizeof(struct kds_task), &tr) != 0;
    struct kds_task *t = kds_va(tr);
    struct kds_graph g;
    int z = 0, one = 1;
    const uint64_t quick = 1UL << 20;

    kds_graph_init(&g, t, 8);
    kds_nop(&g, 0, &one, 1); /* a predecessor that comes later */
    kds_nop(&g, 0, &z, 1);
    fails += expect(&g, KOS_EINVAL, 0, 0, "forward", quick);

    kds_graph_init(&g, t, 8);
    kds_nop(&g, 0, 0, 0);
    kds_nop(&g, far, &z, 1);
    t[0].npred = 1; /* written past the builder: a cycle 0 <-> 1 over two nodes */
    t[0].pred[0] = 1;
    fails += expect(&g, KOS_EINVAL, 0, 0, "cycle", quick);

    kds_graph_init(&g, t, 8);
    uint64_t w[4] = {0, 0, 0, 0};
    kds_unit(&g, KOS_MAX_UNITS - 1, w, 0, 0);
    fails += expect(&g, KOS_EINVAL, 0, 0, "unknown unit", quick);

    kds_graph_init(&g, t, 8);
    int a = kds_nop(&g, 0, 0, 0);
    int b = kds_cpu(&g, 0, ret_arg, 7, 0, 0, &a, 1);
    kds_nop(&g, 0, &b, 1);
    fails += expect(&g, KOS_EFAULT, 1, 7, "cpu fails here", quick);

    kds_graph_init(&g, t, 8);
    a = kds_nop(&g, 0, 0, 0);
    b = kds_cpu(&g, far, ret_arg, 9, 0, 0, &a, 1);
    int c = kds_nop(&g, 0, &b, 1);
    kds_nop(&g, far, &c, 1);
    fails += expect(&g, KOS_EFAULT, 1, 9, "cpu fails on the far node", quick);

    if (n > 1) {
        /* the home waits for no one: a part asleep on another node misses it */
        kds_graph_init(&g, t, 8);
        a = kds_cpu(&g, far, nap, 1UL << 20, 0, 0, 0, 0);
        kds_nop(&g, 0, &a, 1);
        fails += expect(&g, KOS_ETIME, 0, 0, "deadline", 1UL << 18);
    }

    /* the servers still work after all of that */
    kds_graph_init(&g, t, 8);
    a = kds_nop(&g, 0, 0, 0);
    kds_cpu(&g, far, ret_arg, 0, 0, 0, &a, 1);
    fails += expect(&g, KOS_OK, 0, 0, "after", 1UL << 22);
    fails += kds_free(tr) != 0;
    fails += kds_stop() != 0;

    if (n > 1) {
        /* a task on a node with no server */
        fails += kds_start(flags, KDS_ARENA, KDS_ALL_NODES & ~(1u << far)) != 0;
        fails += kds_alloc(0, 8 * sizeof(struct kds_task), &tr) != 0;
        t = kds_va(tr);
        kds_graph_init(&g, t, 8);
        kds_nop(&g, 0, 0, 0);
        kds_cpu(&g, far, ret_arg, 0, 0, 0, &z, 1);
        fails += expect(&g, KOS_ENOENT, 1, 0, "no server", quick);
        fails += kds_stop() != 0;
    }
    kos_printf("kdsbad: %s\n", fails ? "FAIL" : "ok");
    return fails;
}
KOS_APP("kdsbad", kdsbad);

/* ---- one client per node ---- */
static int64_t client(uint64_t k)
{
    uint64_t cycles;
    return chain(k, &cycles);
}

static int kdsmulti(int argc, char **argv)
{
    uint64_t num[1] = {8};
    unsigned nn, n = kos_nodes();
    unsigned flags = kds_args(argc, argv, num, 1, &nn);
    int fails = kds_start(flags, KDS_ARENA, KDS_ALL_NODES) != 0;
    int64_t tid[KOS_MAX_NODES];
    uint64_t t0 = kos_time();
    for (unsigned j = 0; j < n; ++j) {
        tid[j] = kos_spawn(client, num[0], j);
    }
    for (unsigned j = 0; j < n; ++j) {
        int64_t code = 0;
        CHECK(fails, tid[j] >= 0 && !kos_join(tid[j], &code) && !code, "kdsmulti: client on node %u: %ld", j, code);
    }
    REPORT("kdsmulti.cycles", kos_time() - t0);
    fails += kds_stop() != 0;
    kos_printf("kdsmulti: %u clients, %s\n", n, fails ? "FAIL" : "ok");
    return fails;
}
KOS_APP("kdsmulti", kdsmulti);
