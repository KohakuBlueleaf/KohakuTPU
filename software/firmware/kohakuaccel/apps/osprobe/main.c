/* osprobe: the task run queue and the region heaps, self-checked on the node.
 *
 * The host (software/firmware/tools/os.py) boots this with `queue` = a scratch span in
 * this node's DRAM. Each check sets its bit in a pass or a fail mask (a failure
 * also prints); the image exits 0 only when every check passed. Results land at
 *   [queue + 0x00] pass mask   [+0x08] fail mask   [+0x10] cycles per switch
 *   [+0x18] stack bytes used by the deepest worker  [+0x20] heap ops run
 *   [+0x28] checks  [+0x30] tasks spawned  [+0x38] switches  [+0x40] idle passes
 *   [+0x48] cycle counter at the end  [+0x70] at the start  [+0x78] checks so far
 *   [+0x80 + 8i] cycle counter at check i
 * DRAM heap: queue + 64 KB, 1 MB. Staging heap: this mesh's staging + 64 KB, 64 KB.
 */
#include <ka/boot/args.h>
#include <ka/hal/cpu.h>
#include <ka/hal/mem.h>
#include <ka/lib/stdio.h>
#include <ka/os/heap.h>
#include <ka/os/mem.h>
#include <ka/os/task.h>
#include <ka/package/status.h>

#define STACK_WORDS 128 /* 1 KB per task, the minimum; spawn fills every word */
#define NTASK       6

static KA_NOINIT uint64_t stacks[NTASK][STACK_WORDS] __attribute__((aligned(16)));
static struct ka_task tasks[NTASK];
static uint64_t pass, fail, q;
static unsigned nchecks;

static void check(const char *name, int ok, uint64_t a, uint64_t b)
{
    unsigned bit = nchecks++;
    ka_st64(q + 0x78, nchecks);
    ka_st64(q + 0x80 + 8 * bit, ka_cycles());

    /* Only a failure prints. */
    if (ok) {
        pass |= 1UL << bit;
    } else {
        fail |= 1UL << bit;
        ka_printf("osprobe %u FAIL %s %lx %lx\n", bit, name, a, b);
    }
}

static void spawn(unsigned i, const char *name, ka_task_fn fn, void *arg)
{
    ka_task_spawn(&tasks[i], name, fn, arg, stacks[i], sizeof stacks[i]);
}

/* ------------------------------------------------ 1. round robin + exit codes */
static uint8_t order[16];
static unsigned norder;

static int worker(void *arg)
{
    unsigned id = (unsigned)(uintptr_t)arg;
    for (unsigned i = 0; i < 4; ++i) {
        order[norder++] = (uint8_t)(id << 4 | i);
        ka_yield();
    }
    return 100 + (int)id;
}

static void round_robin(void)
{
    norder = 0;
    for (unsigned i = 0; i < 3; ++i) {
        spawn(i, "worker", worker, (void *)(uintptr_t)i);
    }
    uint32_t done = ka_sched_run();
    int ok = done == 3 && norder == 12;
    for (unsigned k = 0; k < 12 && ok; ++k) {
        ok = order[k] == (((k % 3) << 4) | (k / 3));
    }
    check("round robin order", ok, done, norder);
    check("exit codes", tasks[0].code == 100 && tasks[1].code == 101 && tasks[2].code == 102,
          (uint64_t)tasks[0].code, (uint64_t)tasks[2].code);
}

/* ------------------------------------------------ 2. sleep while others run */
static volatile int sleeper_done;
static uint64_t slept, ticks;

static int sleeper(void *arg)
{
    (void)arg;
    uint64_t t0 = ka_cycles();
    ka_sleep(3000);
    slept = ka_cycles() - t0;
    sleeper_done = 1;
    return 0;
}

static int ticker(void *arg)
{
    (void)arg;
    while (!sleeper_done) {
        ++ticks;
        ka_yield();
    }
    return 0;
}

/* ------------------------------------------------ 3. wait on a predicate, and its timeout */
static volatile uint64_t flag;
static uint64_t flag_seen, wait_rc, waited_out, timeout_rc;

static int flag_set(void *ctx) { return *(volatile uint64_t *)ctx != 0; }
static int never(void *ctx)
{
    (void)ctx;
    return 0;
}

static int waiter(void *arg)
{
    (void)arg;
    wait_rc = (uint64_t)ka_wait(flag_set, (void *)&flag, 0);
    flag_seen = flag;
    uint64_t t0 = ka_cycles();
    timeout_rc = (uint64_t)ka_wait(never, 0, 2000);
    waited_out = ka_cycles() - t0;
    return 0;
}

static int setter(void *arg)
{
    (void)arg;
    ka_sleep(1000);
    flag = 0x5E7;
    return 0;
}

/* ------------------------------------------------ 4. a task spawns a task */
static int child_ran;

static int child(void *arg)
{
    (void)arg;
    child_ran = 1;
    return 7;
}

static int parent(void *arg)
{
    (void)arg;
    spawn(5, "child", child, 0);
    ka_yield();
    return 0;
}

static void blocking(void)
{
    sleeper_done = 0;
    ticks = 0;
    flag = 0;
    spawn(0, "sleeper", sleeper, 0);
    spawn(1, "ticker", ticker, 0);
    spawn(2, "waiter", waiter, 0);
    spawn(3, "setter", setter, 0);
    spawn(4, "parent", parent, 0);
    uint32_t done = ka_sched_run();
    check("all six finished", done == 6, done, 0);
    check("sleep >= 3000 cycles", slept >= 3000, slept, 0);
    check("others ran during the sleep", ticks > 0, ticks, 0);
    check("wait woke on its predicate", wait_rc == 0 && flag_seen == 0x5E7, wait_rc, flag_seen);
    check("wait timed out", timeout_rc == KA_ST_TIMEOUT && waited_out >= 2000, timeout_rc,
          waited_out);
    check("spawn from a task", child_ran && tasks[5].code == 7, (uint64_t)child_ran,
          (uint64_t)tasks[5].code);
}

/* ------------------------------------------------ 5. switch cost */
static int pinger(void *arg)
{
    (void)arg;
    for (unsigned i = 0; i < 100; ++i) {
        ka_yield();
    }
    return 0;
}

static uint64_t switch_cost(void)
{
    spawn(0, "ping", pinger, 0);
    spawn(1, "pong", pinger, 0);
    struct ka_sched_stats a, b;
    ka_sched_stats(&a);
    uint64_t t0 = ka_cycles();
    ka_sched_run();
    uint64_t dt = ka_cycles() - t0;
    ka_sched_stats(&b);
    uint64_t n = b.switches - a.switches;
    uint64_t per = n ? dt / n : 0;
    check("ping-pong switches", n == 202, n, per);
    return per;
}

/* ------------------------------------------------ 6. outside any task */
static void no_task(void)
{
    ka_yield();
    uint64_t t0 = ka_cycles();
    int rc = ka_wait(never, 0, 500);
    check("wait outside a task", rc == KA_ST_TIMEOUT && ka_cycles() - t0 >= 500, (uint64_t)rc,
          0);
    check("no task running", ka_task_self() == 0, 0, 0);
}

/* ------------------------------------------------ 7. heaps over real memory */
static uint64_t lcg = 0x2545F4914F6CDD1DUL;
static uint64_t rnd(void)
{
    lcg = lcg * 6364136223846793005UL + 1442695040888963407UL;
    return lcg >> 33;
}

#define LIVE 8 /* blocks live at once in a churn */
static uint64_t heap_ops;

/* `ops` random allocs and frees; each block's first and last word carry its
 * address, re-read before it is freed, so two blocks that overlap are caught. */
static int churn(unsigned region, unsigned ops, uint64_t max_bytes)
{
    struct ka_heap *h = ka_mem_heap(region);
    uint64_t at[LIVE], len[LIVE];
    unsigned n = 0;
    static const uint64_t aligns[4] = {0, 64, 256, 4096};
    for (unsigned i = 0; i < ops; ++i, ++heap_ops) {
        if (n < LIVE && (n == 0 || rnd() % 3)) {
            uint64_t bytes = 1 + rnd() % max_bytes, a = aligns[rnd() % 4], p;
            int rc = ka_mem_alloc(region, bytes, a, i, &p);
            if (rc == KA_ST_NO_MEMORY || rc == KA_ST_NO_SLOTS) {
                continue;
            }
            if (rc || (a && (p & (a - 1))) || (p & (h->granule - 1))) {
                return 1;
            }
            uint64_t l = (bytes + h->granule - 1) & ~(h->granule - 1);
            ka_st64(p, p);
            ka_st64(p + l - 8, ~p);
            at[n] = p;
            len[n++] = l;
        } else {
            unsigned k = (unsigned)(rnd() % n);
            if (ka_ld64(at[k]) != at[k] || ka_ld64(at[k] + len[k] - 8) != ~at[k]) {
                return 2;
            }
            if (ka_mem_free(region, at[k])) {
                return 3;
            }
            at[k] = at[--n];
            len[k] = len[n];
        }
        if (ka_heap_check(h)) {
            return 4;
        }
    }
    while (n) {
        --n;
        if (ka_ld64(at[n]) != at[n] || ka_mem_free(region, at[n])) {
            return 5;
        }
    }
    return ka_heap_check(h) ? 4 : 0;
}

static void heaps(uint64_t dram, uint64_t staging)
{
    check("configure DRAM region", ka_mem_configure(KA_MEM_DRAM, dram, 1 << 20, 64) == 0, dram, 0);
    check("configure staging region", ka_mem_configure(KA_MEM_STAGING, staging, 64 << 10, 64) == 0,
          staging, 0);
    int rc = churn(KA_MEM_DRAM, 24, 16384);
    check("DRAM churn, no overlap", rc == 0, (uint64_t)rc, heap_ops);
    rc = churn(KA_MEM_STAGING, 24, 2048);
    check("staging churn, no overlap", rc == 0, (uint64_t)rc, heap_ops);

    struct ka_heap_stats s;
    ka_heap_stats(ka_mem_heap(KA_MEM_DRAM), &s);
    check("DRAM whole again", s.free == (1 << 20) && s.largest == (1 << 20) && s.blocks == 1,
          s.free, s.blocks);

    uint64_t p, q;
    check("too large -> NO_MEMORY", ka_mem_alloc(KA_MEM_STAGING, (64 << 10) + 1, 0, 0, &p) ==
                                        KA_ST_NO_MEMORY, 0, 0);
    rc = ka_mem_alloc(KA_MEM_STAGING, 100, 4096, 9, &p);
    check("aligned alloc", rc == 0 && !(p & 4095) && ka_heap_find(ka_mem_heap(1), p)->tag == 9,
          p, (uint64_t)rc);
    check("busy region refuses reconfigure",
          ka_mem_configure(KA_MEM_STAGING, staging, 4096, 64) == KA_ST_HEAP_BUSY, 0, 0);
    check("free inside a block -> BAD_FREE", ka_mem_free(KA_MEM_STAGING, p + 64) == KA_ST_BAD_FREE,
          0, 0);
    check("free", ka_mem_free(KA_MEM_STAGING, p) == 0, 0, 0);
    check("double free -> BAD_FREE", ka_mem_free(KA_MEM_STAGING, p) == KA_ST_BAD_FREE, 0, 0);
    check("unconfigured region -> BAD_ARG", ka_mem_alloc(3, 64, 0, 0, &q) == KA_ST_BAD_ARG, 0, 0);

    /* A 3-entry table: a 4 KB-aligned block 64 B off a page leaves a pad and a
     * rest and fills it; the next alloc needs one more entry. */
    static struct ka_block tiny[3];
    struct ka_heap t;
    ka_heap_init(&t, dram + 64, 8192, 64, tiny, 3);
    int r1 = ka_heap_alloc(&t, 64, 4096, 0, &p);
    int r2 = ka_heap_alloc(&t, 64, 0, 0, &q);
    check("full table -> NO_SLOTS", r1 == 0 && r2 == KA_ST_NO_SLOTS && !ka_heap_check(&t),
          (uint64_t)r1, (uint64_t)r2);
}

/* ------------------------------------------------ 8. two tasks sharing a heap */
static int heap_task(void *arg)
{
    unsigned id = (unsigned)(uintptr_t)arg;
    uint64_t mine[6];
    for (unsigned i = 0; i < 6; ++i) {
        if (ka_mem_alloc(KA_MEM_DRAM, 256 + 64 * id, 0, id, &mine[i])) {
            return 1;
        }
        ka_st64(mine[i], id << 8 | i);
        ka_yield();
    }
    for (unsigned i = 0; i < 6; ++i) {
        if (ka_ld64(mine[i]) != (id << 8 | i) || ka_mem_free(KA_MEM_DRAM, mine[i])) {
            return 2;
        }
        ka_yield();
    }
    return 0;
}

static void shared_heap(void)
{
    spawn(0, "heapA", heap_task, (void *)0);
    spawn(1, "heapB", heap_task, (void *)1);
    ka_sched_run();
    struct ka_heap_stats s;
    ka_heap_stats(ka_mem_heap(KA_MEM_DRAM), &s);
    check("two tasks, one heap", tasks[0].code == 0 && tasks[1].code == 0 && s.live == 0 &&
                                     !ka_heap_check(ka_mem_heap(KA_MEM_DRAM)),
          (uint64_t)tasks[0].code, (uint64_t)tasks[1].code);
}

int main(void)
{
    if (ka_boot_check()) {
        return 1;
    }
    q = ka_boot.queue;
    uint64_t mesh = ka_boot.mesh;
    uint64_t staging = 1UL << 39 | mesh << 36 | 0x10000;
    ka_st64(q + 0x70, ka_cycles());
    ka_st64(q + 0x78, 0);
    ka_printf("osprobe q=%lx mesh=%lu\n", q, mesh);

    round_robin();
    blocking();
    uint64_t per = switch_cost();
    no_task();
    heaps(q + 0x10000, staging);
    shared_heap();

    uint32_t deepest = 0;
    for (unsigned i = 0; i < NTASK; ++i) {
        uint32_t u = ka_task_stack_used(&tasks[i]);
        deepest = u > deepest ? u : deepest;
    }
    struct ka_sched_stats ss;
    ka_sched_stats(&ss);
    ka_printf("osprobe %u checks pass %lx fail %lx\n", nchecks, pass, fail);
    ka_st64(q, pass);
    ka_st64(q + 8, fail);
    ka_st64(q + 16, per);
    ka_st64(q + 24, deepest);
    ka_st64(q + 32, heap_ops);
    ka_st64(q + 40, nchecks);
    ka_st64(q + 48, ss.spawned);
    ka_st64(q + 56, ss.switches);
    ka_st64(q + 64, ss.idle_passes);
    ka_st64(q + 72, ka_cycles());
    return fail ? 2 : 0;
}
