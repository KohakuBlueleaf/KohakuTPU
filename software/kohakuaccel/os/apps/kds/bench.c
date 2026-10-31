/* kdsbench M [-flags]: what the dispatcher costs, in cycles (the home's
 * count from submit to the last report, and the client's around kds_run):
 *   one       a graph of one NOP here: submit and answer
 *   chain     M NOPs in a chain here: per task
 *   wide      M independent NOPs here: per task
 *   cpu       M empty CPU tasks in a chain here: per task
 *   hop       M NOPs in a chain alternating node 0 and node 1: per hop (a DEP)
 *   load      M independent NOPs all on the last node: per task (its load,
 *             PART chunks of 64 through the mover, and the run)
 *   alloc     alloc + free here / on the last node, per pair */
#include "kdsapp.h"

static int64_t empty(const uint64_t *a)
{
    (void)a;
    return 0;
}

/* Run g; the home's cycles per `per`, also REPORTed under name. */
static int timed(struct kds_graph *g, const char *name, uint64_t per)
{
    struct kds_result r;
    struct kds_server_stat a, b;
    int fails = 0;
    kds_server_stat(&a);
    uint64_t t0 = kos_time();
    int64_t st = kds_run(g, 1UL << 27, &r);
    uint64_t t = kos_time() - t0;
    kds_server_stat(&b);
    CHECK(fails, st == 0, "kdsbench %s: status %ld task %lu", name, st, r.failed);
    char key[48];
    kos_snprintf(key, sizeof key, "kdsbench.%s", name);
    REPORT(key, r.cycles / per);
    kos_snprintf(key, sizeof key, "kdsbench.%s_client", name);
    REPORT(key, t / per);
    kos_printf("  kdsbench %s: %u tasks, home %lu cycles (%lu each), client %lu\n", name, g->n, r.cycles,
               r.cycles / per, t);
    kds_print_server(key, &a, &b);
    return fails;
}

static int kdsbench(int argc, char **argv)
{
    uint64_t num[1] = {32};
    unsigned nn, n = kos_nodes(), far = n - 1;
    unsigned flags = kds_args(argc, argv, num, 1, &nn);
    unsigned m = (unsigned)(num[0] < 2 ? 2 : num[0]);
    int fails = kds_start(flags, KDS_ARENA, KDS_ALL_NODES) != 0;
    kds_ref tr;
    unsigned cap = m + 1;
    fails += kds_alloc(kos_node(), cap * sizeof(struct kds_task), &tr) != 0;
    struct kds_task *tsk = kds_va(tr);
    struct kds_graph g;
    if (!fails) {
        kds_graph_init(&g, tsk, cap);
        kds_nop(&g, 0, 0, 0);
        fails += timed(&g, "one", 1);

        kds_graph_init(&g, tsk, cap);
        for (unsigned i = 0; i < m; ++i) {
            int p = (int)i - 1;
            kds_nop(&g, 0, &p, i > 0);
        }
        fails += timed(&g, "chain", m);

        kds_graph_init(&g, tsk, cap);
        for (unsigned i = 0; i < m; ++i) {
            kds_nop(&g, 0, 0, 0);
        }
        fails += timed(&g, "wide", m);

        kds_graph_init(&g, tsk, cap);
        for (unsigned i = 0; i < m; ++i) {
            int p = (int)i - 1;
            kds_cpu(&g, 0, empty, 0, 0, 0, &p, i > 0);
        }
        fails += timed(&g, "cpu", m);

        if (n > 1) {
            kds_graph_init(&g, tsk, cap);
            for (unsigned i = 0; i < m; ++i) {
                int p = (int)i - 1;
                kds_nop(&g, i & 1, &p, i > 0);
            }
            fails += timed(&g, "hop", m - 1);

            kds_graph_init(&g, tsk, cap);
            for (unsigned i = 0; i < m; ++i) {
                kds_nop(&g, far, 0, 0);
            }
            fails += timed(&g, "load", m);
        }
    }
    for (unsigned at = 0; at < 2 && !fails; ++at) {
        unsigned node = at ? far : kos_node();
        uint64_t t0 = kos_time();
        for (unsigned i = 0; i < 16; ++i) {
            kds_ref r;
            fails += kds_alloc(node, 4096, &r) != 0;
            fails += kds_free(r) != 0;
        }
        REPORT(at ? "kdsbench.alloc_far" : "kdsbench.alloc_here", (kos_time() - t0) / 16);
    }
    kds_free(tr);
    fails += kds_stop() != 0;
    kos_printf("kdsbench: %u tasks, %s\n", m, fails ? "FAIL" : "ok");
    return fails;
}
KOS_APP("kdsbench", kdsbench);
