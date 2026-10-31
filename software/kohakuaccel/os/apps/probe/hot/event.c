/* kevent: EVENT record layout, unaligned/page-split copies and ready/block cases. */
#include "../../apps.h"

#define CH_EVENT 0x98760022u
extern int64_t hot_event_registers(void *buf, uint64_t n);

static int64_t event_sender(uint64_t unused)
{
    (void)unused;
    uint64_t value = 42;
    return kos_sys(SYS_SEND, kos_node(), CH_EVENT, (uint64_t)&value, 8).st;
}

static int kevent(int argc, char **argv)
{
    (void)argc;
    (void)argv;
    int fails = 0;
    unsigned n = (unsigned)kos_info(KOS_I_NUNITS);
    struct kos_unit_stat units[KOS_MAX_UNITS];
    kos_yield();
    kos_stat(KOS_S_UNITS, units, sizeof units);
    uint8_t *pages = kos_mmap(8192);
    /* Start at an unaligned address so one record straddles a page boundary. */
    uint8_t *dst = pages + 4091;
    memset(dst - 1, 0xa5, 16 * n + 2);
    struct kos_r result = kos_sys(SYS_EVENT, 0, 0, (uint64_t)dst, n + 1);
    CHECK(fails, result.st == 0, "kevent: ready status %ld", result.st);
    for (unsigned i = 0; i < n; ++i) {
        uint64_t pair[2];
        memcpy(pair, dst + 16 * i, sizeof pair);
        CHECK(fails, pair[0] == units[i].done + units[i].faults && pair[1] == units[i].faults,
              "kevent: unit %u got %lu/%lu want %lu/%lu", i, pair[0], pair[1],
              units[i].done + units[i].faults, units[i].faults);
    }
    CHECK(fails, dst[-1] == 0xa5 && dst[16 * n] == 0xa5, "kevent: copy bounds");
    uint64_t sum = 0, records[2 * KOS_MAX_UNITS];
    for (unsigned i = 0; i < 16; ++i) {
        uint64_t start = kos_now();
        kos_sys(SYS_EVENT, 0, 0, (uint64_t)records, n);
        sum += kos_now() - start;
    }
    REPORT("kevent.copy_cycles", sum / 16);
    CHECK(fails, hot_event_registers(records, n) == 0, "kevent: registers");
    /* A missing second page must fail after copying only the valid prefix. */
    kos_munmap(pages + 4096, 4096);
    memset(dst, 0x55, 5);
    result = kos_sys(SYS_EVENT, 0, 0, (uint64_t)dst, n);
    CHECK(fails, result.st == (n ? KOS_EFAULT : 0), "kevent: hole status %ld", result.st);
    for (unsigned i = 0; i < 5; ++i) {
        unsigned want = n ? (unsigned)((units[0].done + units[0].faults) >> (8 * i)) & 255 : 0x55;
        CHECK(fails, dst[i] == want, "kevent: partial copy byte %u", i);
    }
    int64_t child = kos_spawn(event_sender, 0, kos_node());
    result = kos_sys(SYS_EVENT, result.val + 1, 200000, (uint64_t)records,
                     n | (uint64_t)CH_EVENT << 32);
    CHECK(fails, result.st == 1, "kevent: blocked channel wake %ld", result.st);
    uint64_t value = 0;
    CHECK(fails, kos_sys(SYS_RECV, CH_EVENT, (uint64_t)&value, KOS_NOWAIT, 0).st == 8 && value == 42,
          "kevent: wake payload");
    int64_t code = -1;
    int st = kos_join(child, &code);
    CHECK(fails, !st && !code, "kevent: sender join");
    kos_munmap(pages, 4096);
    REPORT("kevent.failures", fails);
    kos_printf("kevent: %s\n", fails ? "FAIL" : "ok");
    return fails;
}
KOS_APP("kevent", kevent);
