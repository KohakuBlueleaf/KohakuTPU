/* stress ROUNDS [threads-per-node] [iters]: locks, atomics and barriers under
 * contention from every node. Each round every worker takes the lock `iters`
 * times and increments a plain shared counter inside it (mutual exclusion
 * and the lock's release/acquire make that exact), adds to an atomic counter,
 * and meets the others at a barrier, where worker 0 checks both totals. */
#include "../apps.h"

struct st {
    kos_lock_t lock;
    uint64_t plain __attribute__((aligned(64)));
    uint64_t atomic __attribute__((aligned(64)));
    uint64_t rounds, w, iters, bad;
    uint64_t t0, t1;
};
static KOS_SHARED struct st *g;

static int64_t worker(uint64_t i)
{
    struct st *p = g;
    for (uint64_t r = 0; r < p->rounds; ++r) {
        for (uint64_t k = 0; k < p->iters; ++k) {
            kos_lock(&p->lock);
            p->plain = p->plain + 1;
            kos_unlock(&p->lock);
            kos_fetch_add(&p->atomic, 1);
        }
        kos_barrier(40, p->w);
        if (i == 0) {
            uint64_t want = (r + 1) * p->w * p->iters;
            uint64_t a = kos_load(&p->atomic);
            if (p->plain != want || a != want) {
                ++p->bad;
                kos_printf("FAIL stress round %lu: plain %lu atomic %lu want %lu\n", r, p->plain, a, want);
            }
        }
        kos_barrier(41, p->w);
    }
    return 0;
}

static int stress(int argc, char **argv)
{
    int fails = 0;
    if (!kos_shared_ok()) {
        kos_printf("stress: needs shared memory\n");
        return 0;
    }
    struct st *p = kos_shalloc(sizeof *p);
    p->rounds = arg_u(argc, argv, 1, 4);
    p->w = kos_nodes() * arg_u(argc, argv, 2, 1);
    p->iters = arg_u(argc, argv, 3, 8);
    p->plain = 0;
    p->bad = 0;
    kos_atomic(&p->atomic, KOS_A_SWAP, 0, 0);
    kos_atomic(&p->lock.next, KOS_A_SWAP, 0, 0);
    *kos_unc(&p->lock.serving) = 0;
    for (unsigned j = 0; j < KOS_LOCK_SLOTS; ++j) {
        kos_unc(p->lock.choosing)[j] = 0;
        kos_unc(p->lock.number)[j] = 0;
    }
    g = p;
    uint64_t t0 = kos_time();
    fails += run_workers(worker, (unsigned)p->w);
    uint64_t t = kos_time() - t0;
    uint64_t ops = p->rounds * p->w * p->iters;
    fails += (int)p->bad;
    CHECK(fails, p->plain == ops, "stress: plain counter %lu want %lu", p->plain, ops);
    REPORT("stress.lock_ops", ops);
    REPORT("stress.cycles", t);
    REPORT("stress.cycles_per_lock_op", ops ? t / ops : 0);
    kos_printf("stress: %lu rounds x %lu workers x %lu, %s locks: %s\n", p->rounds, p->w, p->iters,
               (kos_info(KOS_I_FLAGS) & KOS_F_LOCK_BAKERY) ? "bakery" : "ticket", fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("stress", stress);
