/* msgsum N: a reduction with no shared memory. One worker per node fills
 * N values in its own private pages (kos_mmap), sums them, and sends the
 * partial to node 0 over a channel; node 0 checks the total. */
#include "../apps.h"

#define CH_PART 98

static int64_t worker(uint64_t arg)
{
    uint64_t n = arg >> 8, node = arg & 0xff, nodes = kos_nodes();
    uint64_t lo = n * node / nodes, hi = n * (node + 1) / nodes;
    uint64_t *a = kos_mmap((hi - lo) * 8 + 8);
    if (!a) {
        return 1;
    }
    for (uint64_t k = lo; k < hi; ++k) {
        a[k - lo] = value_at(k);
    }
    uint64_t s[2] = {node, 0};
    for (uint64_t k = lo; k < hi; ++k) {
        s[1] += a[k - lo];
    }
    return kos_send(0, CH_PART, s, 16) ? 2 : 0;
}

static int msgsum(int argc, char **argv)
{
    int fails = 0;
    uint64_t n = arg_u(argc, argv, 1, 4096);
    unsigned nodes = kos_nodes();
    int64_t tid[KOS_MAX_NODES];
    uint64_t t0 = kos_time();
    for (unsigned j = 0; j < nodes; ++j) {
        tid[j] = kos_spawn(worker, n << 8 | j, j);
    }
    uint64_t total = 0, seen = 0;
    for (unsigned j = 0; j < nodes; ++j) {
        uint64_t s[2];
        unsigned src;
        if (kos_recv(CH_PART, s, 0, &src) == 16 && s[0] == src) {
            total += s[1];
            seen |= 1UL << src;
        }
    }
    uint64_t t = kos_time() - t0;
    for (unsigned j = 0; j < nodes; ++j) {
        int64_t code = 0;
        if (tid[j] < 0 || kos_join(tid[j], &code) || code) {
            ++fails;
            kos_printf("FAIL msgsum worker %u exit %ld\n", j, code);
        }
    }
    uint64_t want = 0;
    for (uint64_t k = 0; k < n; ++k) {
        want += value_at(k);
    }
    CHECK(fails, total == want && seen == (1UL << nodes) - 1, "msgsum: %lu want %lu, nodes seen %lx", total,
          want, seen);
    REPORT("msgsum.n", n);
    REPORT("msgsum.cycles", t);
    kos_printf("msgsum: %lu values over %u nodes, %s\n", n, nodes, fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("msgsum", msgsum);
