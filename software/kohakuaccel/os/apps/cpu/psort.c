/* psort N [threads-per-node]: parallel merge sort over shared memory.
 * W workers (the largest power of two the nodes hold) each heap-sort their
 * chunk in place, then log2(W) rounds merge neighbouring runs between two
 * buffers, a barrier between rounds. Checked: sorted, and the same multiset
 * (sum and xor of the input). The one-worker run is the baseline. */
#include "../apps.h"

struct ps {
    uint64_t n, w;
    uint64_t *buf[2];
    uint64_t t0, t1;
    int final; /* which buffer holds the result */
};
static KOS_SHARED struct ps *g;

static void sift(uint64_t *a, uint64_t start, uint64_t end)
{
    uint64_t root = start;
    while (2 * root + 1 < end) {
        uint64_t c = 2 * root + 1;
        if (c + 1 < end && a[c] < a[c + 1]) {
            ++c;
        }
        if (a[root] >= a[c]) {
            return;
        }
        uint64_t t = a[root];
        a[root] = a[c];
        a[c] = t;
        root = c;
    }
}

static void heapsort(uint64_t *a, uint64_t n)
{
    if (n < 2) {
        return;
    }
    for (uint64_t s = n / 2; s-- > 0;) {
        sift(a, s, n);
    }
    for (uint64_t e = n - 1; e > 0; --e) {
        uint64_t t = a[0];
        a[0] = a[e];
        a[e] = t;
        sift(a, 0, e);
    }
}

static int64_t worker(uint64_t i)
{
    struct ps *p = g;
    uint64_t lo = chunk(p->n, p->w, i), hi = chunk(p->n, p->w, i + 1);
    uint64_t st = 0x5eed + i;
    for (uint64_t k = lo; k < hi; ++k) {
        p->buf[0][k] = kos_mix(&st) >> 16;
    }
    kos_barrier(20, p->w);
    if (i == 0) {
        p->t0 = kos_time();
    }
    heapsort(p->buf[0] + lo, hi - lo);
    int src = 0;
    for (uint64_t width = 1; width < p->w; width *= 2) {
        kos_barrier(21, p->w);
        if (i % (2 * width) == 0) {
            uint64_t a = chunk(p->n, p->w, i), m = chunk(p->n, p->w, i + width);
            uint64_t b = chunk(p->n, p->w, i + 2 * width);
            uint64_t *s = p->buf[src], *d = p->buf[!src];
            uint64_t x = a, y = m, o = a;
            while (x < m && y < b) {
                d[o++] = s[x] <= s[y] ? s[x++] : s[y++];
            }
            while (x < m) {
                d[o++] = s[x++];
            }
            while (y < b) {
                d[o++] = s[y++];
            }
        }
        src = !src;
    }
    kos_barrier(22, p->w);
    if (i == 0) {
        p->t1 = kos_time();
        p->final = src;
    }
    return 0;
}

static uint64_t run(struct ps *p, uint64_t w, int *fails)
{
    p->w = w;
    g = p;
    *fails += run_workers(worker, (unsigned)w);
    uint64_t sum = 0, x = 0, want_sum = 0, want_x = 0;
    uint64_t *r = p->buf[p->final];
    for (uint64_t i = 0; i < w; ++i) {
        uint64_t st = 0x5eed + i;
        for (uint64_t k = chunk(p->n, w, i); k < chunk(p->n, w, i + 1); ++k) {
            uint64_t v = kos_mix(&st) >> 16;
            want_sum += v;
            want_x ^= v;
        }
    }
    uint64_t unsorted = 0;
    for (uint64_t k = 0; k < p->n; ++k) {
        sum += r[k];
        x ^= r[k];
        unsorted += k && r[k - 1] > r[k];
    }
    CHECK(*fails, !unsorted && sum == want_sum && x == want_x,
          "psort w=%lu: %lu out of order, sum %lx/%lx xor %lx/%lx", w, unsorted, sum, want_sum, x, want_x);
    return p->t1 - p->t0;
}

static int psort(int argc, char **argv)
{
    int fails = 0;
    if (!kos_shared_ok()) {
        kos_printf("psort: needs shared memory\n");
        return 0;
    }
    uint64_t n = arg_u(argc, argv, 1, 8192), tpn = arg_u(argc, argv, 2, 1);
    uint64_t w = 1;
    while (w * 2 <= kos_nodes() * tpn) {
        w *= 2;
    }
    struct ps *p = kos_shalloc(sizeof *p);
    p->n = n;
    p->buf[0] = kos_shalloc(n * 8);
    p->buf[1] = kos_shalloc(n * 8);
    if (!p->buf[0] || !p->buf[1]) {
        kos_printf("FAIL psort: shared memory for %lu values\n", n);
        return 1;
    }
    uint64_t t1 = run(p, 1, &fails);
    uint64_t tw = run(p, w, &fails);
    REPORT("psort.n", n);
    REPORT("psort.workers", w);
    REPORT("psort.cycles_1", t1);
    REPORT("psort.cycles_w", tw);
    REPORT("psort.speedup_x100", tw ? t1 * 100 / tw : 0);
    kos_printf("psort: %lu values, %lu workers, %s\n", n, w, fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("psort", psort);
