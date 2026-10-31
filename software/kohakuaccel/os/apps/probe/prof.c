/* kprof [iters]: where a system call's cycles go, on a kernel built with
 * -D KOS_PROF (kernel.h): the trap path's stages, averaged over `iters`
 * calls of each kind, for
 *   self          a null call (SYS_SELF), with the caller's own stamp
 *   yield         a yield with one other ready thread
 *   send_here     a channel send to this node
 *   recv_ready    a receive that finds its message (KOS_NOWAIT)
 *   send_remote   a channel send to node 1
 * Stages: enter (caller's stamp to the first saves; self only), save, to_c,
 * call (the system call itself), poll, schedule, to_resume; out (self only)
 * is the rest of the caller's round trip: the restores, sret, the stamp. */
#include "../apps.h"

#define CH_PROF 92
#define N_STAGE 7

static const char *const stage[N_STAGE] = {"enter", "save", "to_c", "call", "poll", "schedule", "to_resume"};

static uint64_t cyc(void)
{
    uint64_t c;
    __asm__ volatile("csrr %0, cycle" : "=r"(c));
    return c;
}

static int64_t prof(uint64_t a) { return kos_sys(SYS_INFO, KOS_I_PROF, a, 0, 0).st; }

static int fails;

/* Print the stages of the `want` calls since the selection: their average
 * each, and their sum. */
static uint64_t report(const char *what, uint64_t first, uint64_t want)
{
    uint64_t n = (uint64_t)prof(N_STAGE), sum = 0;
    CHECK(fails, n == want, "kprof: %s: %lu calls profiled, want %lu", what, n, want);
    char name[48];
    for (unsigned i = first; i < N_STAGE; ++i) {
        uint64_t v = n ? (uint64_t)prof(i) / n : 0;
        CHECK(fails, v > 0 && v < 1000000, "kprof: %s.%s average %lu", what, stage[i], v);
        sum += v;
        kos_snprintf(name, sizeof name, "kprof.%s.%s", what, stage[i]);
        REPORT(name, v);
    }
    kos_snprintf(name, sizeof name, "kprof.%s.calls", what);
    REPORT(name, n);
    return sum;
}

static int64_t spinner(uint64_t n)
{
    for (uint64_t i = 0; i < n; ++i) {
        kos_yield();
    }
    return 0;
}

static int64_t drainer(uint64_t n)
{
    uint64_t v;
    for (uint64_t i = 0; i < n; ++i) {
        if (kos_sys(SYS_RECV, CH_PROF, (uint64_t)&v, 0, 0).st != 8) {
            return 1;
        }
    }
    return 0;
}

static int kprof(int argc, char **argv)
{
    fails = 0;
    uint64_t iters = arg_u(argc, argv, 1, 32), v = 7;
    unsigned me = kos_node();
    if (prof(0x100 + SYS_SELF) != KOS_OK) {
        kos_printf("kprof: not a KOS_PROF kernel\n");
        return 1;
    }
    uint64_t total = 0;
    for (uint64_t i = 0; i < iters; ++i) {
        uint64_t c0 = cyc();
        kos_sys(SYS_SELF, c0, 0, 0, 0);
        total += cyc() - c0;
    }
    uint64_t inside = report("self", 0, iters);
    REPORT("kprof.self.out", total / iters - inside);
    REPORT("kprof.self.total", total / iters);

    int64_t sp = kos_spawn(spinner, iters + 1, me);
    kos_yield();
    prof(0x100 + SYS_YIELD);
    for (uint64_t i = 0; i < iters; ++i) {
        kos_yield();
    }
    report("yield", 1, 2 * iters); /* the spinner's yields count too */
    CHECK(fails, kos_join(sp, 0) == 0, "kprof: spinner join");

    uint64_t n = iters < 32 ? iters : 32; /* the channel queue holds 64 */
    prof(0x100 + SYS_SEND);
    for (uint64_t i = 0; i < n; ++i) {
        kos_sys(SYS_SEND, me, CH_PROF, (uint64_t)&v, 8);
    }
    report("send_here", 1, n);
    prof(0x100 + SYS_RECV);
    for (uint64_t i = 0; i < n; ++i) {
        CHECK(fails, kos_sys(SYS_RECV, CH_PROF, (uint64_t)&v, KOS_NOWAIT, 0).st == 8, "kprof: recv %lu", i);
    }
    report("recv_ready", 1, n);

    if (kos_nodes() > 1 && me != 1) {
        int64_t d = kos_spawn(drainer, n, 1);
        prof(0x100 + SYS_SEND);
        for (uint64_t i = 0; i < n; ++i) {
            kos_sys(SYS_SEND, 1, CH_PROF, (uint64_t)&v, 8);
        }
        report("send_remote", 1, n);
        CHECK(fails, kos_join(d, 0) == 0, "kprof: drainer join");
    }
    kos_printf("kprof: %s\n", fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("kprof", kprof);
