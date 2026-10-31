/* preduce N [threads-per-node]: parallel sum over shared memory.
 * Each worker writes its chunk, then (after a barrier) sums the NEXT worker's
 * chunk, so every partial reads values another node wrote. The same run with
 * one worker is the baseline the speedup is against. */
#include "../apps.h"

struct pr {
    uint64_t n, w;
    uint64_t *a;
    uint64_t *part; /* one 64-byte line per worker */
    uint64_t t0, t1;
};
static KOS_SHARED struct pr *g;

static int64_t worker(uint64_t i)
{
    struct pr *p = g;
    uint64_t lo = chunk(p->n, p->w, i), hi = chunk(p->n, p->w, i + 1);
    for (uint64_t k = lo; k < hi; ++k) {
        p->a[k] = value_at(k);
    }
    kos_barrier(10, p->w);
    if (i == 0) {
        p->t0 = kos_time();
    }
    uint64_t j = (i + 1) % p->w;
    uint64_t jlo = chunk(p->n, p->w, j), jhi = chunk(p->n, p->w, j + 1), s = 0;
    for (uint64_t k = jlo; k < jhi; ++k) {
        s += p->a[k];
    }
    p->part[i * 8] = s;
    kos_barrier(11, p->w);
    if (i == 0) {
        p->t1 = kos_time();
    }
    return 0;
}

static uint64_t run(struct pr *p, uint64_t w, int *fails, uint64_t expect)
{
    p->w = w;
    g = p;
    *fails += run_workers(worker, (unsigned)w);
    uint64_t total = 0;
    for (uint64_t i = 0; i < w; ++i) {
        total += p->part[i * 8];
    }
    CHECK(*fails, total == expect, "preduce w=%lu sum %lu want %lu", w, total, expect);
    return p->t1 - p->t0;
}

static int preduce(int argc, char **argv)
{
    int fails = 0;
    if (!kos_shared_ok()) {
        kos_printf("preduce: needs shared memory\n");
        return 0;
    }
    uint64_t n = arg_u(argc, argv, 1, 16384), tpn = arg_u(argc, argv, 2, 1);
    uint64_t w = kos_nodes() * tpn;
    struct pr *p = kos_shalloc(sizeof *p);
    p->n = n;
    p->a = kos_shalloc(n * 8);
    p->part = kos_shalloc(64 * 8 * 64);
    if (!p->a || !p->part) {
        kos_printf("FAIL preduce: shared memory for %lu values\n", n);
        return 1;
    }
    uint64_t expect = 0;
    for (uint64_t k = 0; k < n; ++k) {
        expect += value_at(k);
    }
    uint64_t t1 = run(p, 1, &fails, expect);
    uint64_t tw = run(p, w, &fails, expect);
    REPORT("preduce.n", n);
    REPORT("preduce.workers", w);
    REPORT("preduce.cycles_1", t1);
    REPORT("preduce.cycles_w", tw);
    REPORT("preduce.speedup_x100", tw ? t1 * 100 / tw : 0);
    kos_printf("preduce: %lu values, %lu workers, %s\n", n, w, fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("preduce", preduce);
