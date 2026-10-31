/* workq TASKS [threads-per-node]: dynamic work distribution. Workers take
 * task numbers from one shared counter (an atomic at its home node) until
 * none are left; a task is the Collatz step count summed over a 64-number
 * block whose cost varies. Checked: every task done once, the total equal to
 * the serial sum. Reports how the tasks spread over the workers. */
#include "../apps.h"

struct wq {
    uint64_t tasks, w;
    uint64_t next;      /* the shared task counter (atomics only) */
    uint64_t *done;     /* per task: 1 + worker; a line per 8 tasks is written by many: atomics */
    uint64_t *per;      /* per worker: tasks taken, sum (one line each) */
    uint64_t t0, t1;
};
static KOS_SHARED struct wq *g;

static uint64_t task(uint64_t t)
{
    uint64_t s = 0;
    for (uint64_t v0 = t * 64 + 1; v0 <= t * 64 + 64; ++v0) {
        uint64_t v = v0;
        while (v != 1) {
            v = (v & 1) ? 3 * v + 1 : v / 2;
            ++s;
        }
    }
    return s;
}

static int64_t worker(uint64_t i)
{
    struct wq *p = g;
    uint64_t taken = 0, sum = 0;
    for (;;) {
        uint64_t t = kos_fetch_add(&p->next, 1);
        if (t >= p->tasks) {
            break;
        }
        sum += task(t);
        kos_fetch_add(&p->done[t], 1 + i);
        ++taken;
    }
    p->per[i * 8] = taken;
    p->per[i * 8 + 1] = sum;
    return 0;
}

static int workq(int argc, char **argv)
{
    int fails = 0;
    if (!kos_shared_ok()) {
        kos_printf("workq: needs shared memory\n");
        return 0;
    }
    uint64_t tasks = arg_u(argc, argv, 1, 64), tpn = arg_u(argc, argv, 2, 1);
    uint64_t w = kos_nodes() * tpn;
    struct wq *p = kos_shalloc(sizeof *p);
    p->tasks = tasks;
    p->w = w;
    p->done = kos_shalloc(tasks * 8);
    p->per = kos_shalloc(64 * 64);
    if (!p->done || !p->per) {
        kos_printf("FAIL workq: shared memory\n");
        return 1;
    }
    kos_atomic(&p->next, KOS_A_SWAP, 0, 0);
    for (uint64_t t = 0; t < tasks; ++t) {
        kos_atomic(&p->done[t], KOS_A_SWAP, 0, 0);
    }
    uint64_t want = 0;
    uint64_t ts = kos_time();
    for (uint64_t t = 0; t < tasks; ++t) {
        want += task(t);
    }
    uint64_t serial = kos_time() - ts;
    g = p;
    uint64_t t0 = kos_time();
    fails += run_workers(worker, (unsigned)w);
    uint64_t par = kos_time() - t0;
    uint64_t sum = 0, n = 0, once = 0;
    for (uint64_t i = 0; i < w; ++i) {
        n += p->per[i * 8];
        sum += p->per[i * 8 + 1];
        kos_printf("workq: worker %lu (node %lu) took %lu tasks\n", i, i % kos_nodes(), p->per[i * 8]);
    }
    for (uint64_t t = 0; t < tasks; ++t) {
        uint64_t d = kos_load(&p->done[t]);
        once += d >= 1 && d <= w;
    }
    CHECK(fails, n == tasks && once == tasks && sum == want,
          "workq: %lu tasks taken, %lu done once, sum %lu want %lu", n, once, sum, want);
    REPORT("workq.tasks", tasks);
    REPORT("workq.workers", w);
    REPORT("workq.cycles_serial", serial);
    REPORT("workq.cycles_parallel", par);
    REPORT("workq.speedup_x100", par ? serial * 100 / par : 0);
    kos_printf("workq: %lu tasks over %lu workers, %s\n", tasks, w, fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("workq", workq);
