/* pmatmul M [threads-per-node]: C = A x B over int64, M x M, rows of C split
 * over the workers (each worker's rows a whole number of cache lines).
 * Checked against the one-worker result entry for entry. */
#include "../apps.h"

struct pm {
    uint64_t m, w;
    int64_t *a, *b, *c, *ref;
    uint64_t t0, t1;
};
static KOS_SHARED struct pm *g;

static int64_t worker(uint64_t i)
{
    struct pm *p = g;
    uint64_t m = p->m;
    /* rows in groups of 8 so a worker's rows never share a line with another's */
    uint64_t r0 = chunk(m, p->w, i), r1 = chunk(m, p->w, i + 1);
    for (uint64_t r = r0; r < r1; ++r) {
        for (uint64_t k = 0; k < m; ++k) {
            p->a[r * m + k] = (int64_t)((r * 31 + k * 17) % 13) - 6;
            p->b[r * m + k] = (int64_t)((r * 7 + k * 11) % 9) - 4;
        }
    }
    kos_barrier(30, p->w);
    if (i == 0) {
        p->t0 = kos_time();
    }
    for (uint64_t r = r0; r < r1; ++r) {
        for (uint64_t col = 0; col < m; ++col) {
            int64_t s = 0;
            for (uint64_t k = 0; k < m; ++k) {
                s += p->a[r * m + k] * p->b[k * m + col];
            }
            p->c[r * m + col] = s;
        }
    }
    kos_barrier(31, p->w);
    if (i == 0) {
        p->t1 = kos_time();
    }
    return 0;
}

static uint64_t run(struct pm *p, uint64_t w, int *fails)
{
    p->w = w;
    g = p;
    *fails += run_workers(worker, (unsigned)w);
    return p->t1 - p->t0;
}

static int pmatmul(int argc, char **argv)
{
    int fails = 0;
    if (!kos_shared_ok()) {
        kos_printf("pmatmul: needs shared memory\n");
        return 0;
    }
    uint64_t m = KOS_ALIGN_UP(arg_u(argc, argv, 1, 32), 8), tpn = arg_u(argc, argv, 2, 1);
    uint64_t w = kos_nodes() * tpn;
    struct pm *p = kos_shalloc(sizeof *p);
    p->m = m;
    p->a = kos_shalloc(m * m * 8);
    p->b = kos_shalloc(m * m * 8);
    p->c = kos_shalloc(m * m * 8);
    p->ref = kos_shalloc(m * m * 8);
    if (!p->a || !p->b || !p->c || !p->ref) {
        kos_printf("FAIL pmatmul: shared memory for M=%lu\n", m);
        return 1;
    }
    uint64_t t1 = run(p, 1, &fails);
    memcpy(p->ref, p->c, m * m * 8);
    memset(p->c, 0, m * m * 8);
    uint64_t tw = run(p, w, &fails);
    uint64_t bad = 0;
    for (uint64_t i = 0; i < m * m; ++i) {
        bad += p->c[i] != p->ref[i];
    }
    CHECK(fails, bad == 0, "pmatmul: %lu of %lu entries differ from the one-worker run", bad, m * m);
    REPORT("pmatmul.m", m);
    REPORT("pmatmul.workers", w);
    REPORT("pmatmul.cycles_1", t1);
    REPORT("pmatmul.cycles_w", tw);
    REPORT("pmatmul.speedup_x100", tw ? t1 * 100 / tw : 0);
    REPORT("pmatmul.macs_per_kcycle_w", tw ? m * m * m * 1000 / tw : 0);
    kos_printf("pmatmul: M=%lu, %lu workers, %s\n", m, w, fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("pmatmul", pmatmul);
