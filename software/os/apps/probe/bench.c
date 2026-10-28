/* bench [iters]: what the kernel's primitives cost, in node cycles (global
 * time), each averaged over `iters`:
 *   syscall       a null system call (SYS_SELF)
 *   l0            a kernel -> L0 call round trip
 *   yield         a yield with one other ready thread (two switches)
 *   spawn_local / spawn_remote   spawn + join of an empty thread
 *   atomic_local / atomic_remote a fetch_add whose home is this / another node
 *   chan_rtt      a channel message to node 1 and its echo back
 *   barrier       a barrier of one thread per node
 *   release / acquire            the cache sweeps
 *   mmap          a 4-page mmap */
#include "../apps.h"

#define CH_PING 90
#define CH_PONG 91

static uint64_t iters;

static int64_t nothing(uint64_t a) { return (int64_t)a; }

static int64_t spinner(uint64_t n)
{
    for (uint64_t i = 0; i < n; ++i) {
        kos_yield();
    }
    return 0;
}

static int64_t echo(uint64_t n)
{
    uint64_t v;
    unsigned src;
    for (uint64_t i = 0; i < n; ++i) {
        if (kos_recv(CH_PING, &v, 0, &src) != 8) {
            return 1;
        }
        kos_send(src, CH_PONG, &v, 8);
    }
    return 0;
}

static int64_t bar_worker(uint64_t n)
{
    for (uint64_t i = 0; i < n; ++i) {
        kos_barrier(70, kos_nodes());
    }
    return 0;
}

/* a shared word whose home is `node` */
static volatile uint64_t *word_at_home(uint8_t *pages, unsigned node)
{
    uint64_t base = kos_info(KOS_I_SHARED);
    for (unsigned k = 0; k < KOS_MAX_NODES; ++k) {
        uint8_t *p = pages + 4096 * k;
        if ((((uint64_t)p - base) >> 12) % kos_nodes() == node) {
            return (volatile uint64_t *)p;
        }
    }
    return 0;
}

static int bench(int argc, char **argv)
{
    int fails = 0;
    iters = arg_u(argc, argv, 1, 16);
    unsigned me = kos_node(), n = kos_nodes();
    uint64_t t;

    t = kos_time();
    for (uint64_t i = 0; i < iters; ++i) {
        kos_sys(SYS_SELF, 0, 0, 0, 0);
    }
    REPORT("bench.syscall", (kos_time() - t) / iters);
    REPORT("bench.l0", kos_info(KOS_I_L0RTT));

    int64_t sp = kos_spawn(spinner, iters, me);
    kos_yield(); /* let it start */
    t = kos_time();
    for (uint64_t i = 0; i < iters; ++i) {
        kos_yield();
    }
    REPORT("bench.yield", (kos_time() - t) / iters);
    CHECK(fails, kos_join(sp, 0) == 0, "bench: spinner join");

    for (unsigned remote = 0; remote < 2; ++remote) {
        if (remote && n < 2) {
            break;
        }
        unsigned node = remote ? 1 : me;
        t = kos_time();
        for (uint64_t i = 0; i < iters; ++i) {
            int64_t code = -1;
            int64_t tid = kos_spawn(nothing, i, node);
            if (tid < 0 || kos_join(tid, &code) || code != (int64_t)i) {
                ++fails;
                kos_printf("FAIL bench: spawn on node %u: tid %ld exit %ld\n", node, tid, code);
                break;
            }
        }
        REPORT(remote ? "bench.spawn_remote" : "bench.spawn_local", (kos_time() - t) / iters);
    }

    if (kos_shared_ok()) {
        uint8_t *pages = kos_shalloc(4096 * (KOS_MAX_NODES + 1));
        pages = (uint8_t *)KOS_ALIGN_UP((uint64_t)pages, 4096);
        for (unsigned remote = 0; remote < 2; ++remote) {
            if (remote && n < 2) {
                break;
            }
            volatile uint64_t *w = word_at_home(pages, remote ? (me + 1) % n : me);
            kos_atomic(w, KOS_A_SWAP, 0, 0);
            t = kos_time();
            for (uint64_t i = 0; i < iters; ++i) {
                kos_fetch_add(w, 1);
            }
            REPORT(remote ? "bench.atomic_remote" : "bench.atomic_local", (kos_time() - t) / iters);
            CHECK(fails, kos_load(w) == iters, "bench: atomic count %lu want %lu", kos_load(w), iters);
        }
        t = kos_time();
        for (uint64_t i = 0; i < iters; ++i) {
            kos_release();
        }
        REPORT("bench.release", (kos_time() - t) / iters);
        t = kos_time();
        for (uint64_t i = 0; i < iters; ++i) {
            kos_acquire();
        }
        REPORT("bench.acquire", (kos_time() - t) / iters);
    }

    if (n > 1) {
        int64_t e = kos_spawn(echo, iters, 1);
        t = kos_time();
        for (uint64_t i = 0; i < iters; ++i) {
            uint64_t v = i * 7, back = 0;
            unsigned src;
            kos_send(1, CH_PING, &v, 8);
            if (kos_recv(CH_PONG, &back, 0, &src) != 8 || back != v || src != 1) {
                ++fails;
                kos_printf("FAIL bench: echo %lu got %lu from %u\n", v, back, src);
                break;
            }
        }
        REPORT("bench.chan_rtt", (kos_time() - t) / iters);
        CHECK(fails, kos_join(e, 0) == 0, "bench: echo join");
    }

    int64_t bt[KOS_MAX_NODES];
    for (unsigned j = 1; j < n; ++j) {
        bt[j] = kos_spawn(bar_worker, iters + 1, j);
    }
    kos_barrier(70, n); /* everyone started */
    t = kos_time();
    for (uint64_t i = 0; i < iters; ++i) {
        kos_barrier(70, n);
    }
    REPORT("bench.barrier", (kos_time() - t) / iters);
    for (unsigned j = 1; j < n; ++j) {
        CHECK(fails, kos_join(bt[j], 0) == 0, "bench: barrier worker %u", j);
    }

    t = kos_time();
    void *m = kos_mmap(4 * 4096);
    REPORT("bench.mmap", kos_time() - t);
    CHECK(fails, m != 0, "bench: mmap");
    if (m) {
        ((volatile uint64_t *)m)[0] = 1;
        ((volatile uint64_t *)m)[4 * 512 - 1] = 2;
    }
    kos_printf("bench: %s\n", fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("bench", bench);
