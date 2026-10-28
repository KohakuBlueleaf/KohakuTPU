/* khot: trap registers, stale translations, channel FIFO and blocked wake. */
#include "../../apps.h"

extern int64_t hot_registers(uint64_t value);

static int64_t receiver(uint64_t chan)
{
    uint64_t v = 0;
    int64_t n = kos_sys(SYS_RECV, chan, (uint64_t)&v, 200000, 0).st;
    return n == 8 && v == 0x12345678 ? 0 : 1;
}

static int khot(int argc, char **argv)
{
    unsigned which = (unsigned)arg_u(argc, argv, 1, 0), me = kos_node();
    int fails = 0;
    if (!which || which == 2) {
        int64_t child = kos_spawn(hot_registers, 0x123456, me), code = -1;
        CHECK(fails, hot_registers(0x654321) == 0, "khot: parent callee-save");
        int st = kos_join(child, &code);
        CHECK(fails, !st && !code, "khot: child callee-save");
    }
    if (!which || which == 4) {
        void *p = kos_mmap(4096);
        int64_t before = kos_vtop(p, 8);
        int st = kos_munmap(p, 4096);
        int64_t after = kos_vtop(p, 8);
        CHECK(fails, before > 0 && !st && after == KOS_EINVAL, "khot: stale translation %ld", after);
    }
    if (!which || which == 5) {
        int64_t child = kos_spawn(receiver, 0xfedcba98u, me), code = -1;
        kos_yield();
        uint64_t v = 0x12345678;
        int64_t st = kos_sys(SYS_SEND, me, 0xfedcba98u, (uint64_t)&v, 8).st;
        int joined = kos_join(child, &code);
        CHECK(fails, !st && !joined && !code, "khot: lost channel wake");
    }
    if (!which || which == 6) {
        /* Colliding arbitrary channel keys, interleaved FIFOs, then slot reuse. */
        for (unsigned round = 0; round < 3; ++round) {
            for (uint64_t i = 0; i < 12; ++i) {
                uint64_t key = (i & 1) ? 0xffff0040u : 0x8000003fu;
                CHECK(fails, !kos_sys(SYS_SEND, me, key, (uint64_t)&i, 8).st, "khot: send");
            }
            for (unsigned parity = 0; parity < 2; ++parity) {
                uint64_t key = parity ? 0xffff0040u : 0x8000003fu;
                for (uint64_t i = parity; i < 12; i += 2) {
                    struct kos_r ev = kos_sys(SYS_EVENT, 0, 0, 0, key << 32);
                    uint64_t v = ~0UL;
                    int64_t st = kos_sys(SYS_RECV, key, (uint64_t)&v, KOS_NOWAIT, 0).st;
                    CHECK(fails, ev.st == (int64_t)((12 - i + 1) / 2) && st == 8 && v == i,
                          "khot: FIFO/count at %lu got %lu count %ld", i, v, ev.st);
                }
            }
        }
    }
    if (which == 8) {
        uint64_t base = kos_info(KOS_I_SHARED);
        volatile uint64_t *first = (volatile uint64_t *)base;
        volatile uint64_t *second = (volatile uint64_t *)(base + 4096);
        *first = 0x1111;
        *second = 0x2222;
        uint64_t got = 0;
        int64_t sent = kos_sys(SYS_SEND, me, 123, (uint64_t)second, 8).st;
        int64_t received = kos_sys(SYS_RECV, 123, (uint64_t)&got, KOS_NOWAIT, 0).st;
        CHECK(fails, !sent && received == 8 && got == 0x2222, "khot: L0 superpage %lu", got);
    }
    if (!which || which == 1) {
        struct kos_r self = kos_sys(SYS_SELF, 0, 0xabcdef, 0, 0);
        CHECK(fails, KOS_TID_NODE(self.st) == me && self.val == 0xabcdef, "khot: leaf ABI");
        uint64_t before = kos_now();
        uint64_t stamp = kos_time();
        CHECK(fails, stamp >= before && stamp <= kos_now(), "khot: leaf time");
    }
    if (!which || which == 1) {
        uint64_t total = 0;
        for (unsigned i = 0; i < 32; ++i) {
            uint64_t before = kos_now();
            kos_sys(SYS_SELF, 0, 0, 0, 0);
            total += kos_now() - before;
        }
        REPORT("khot.self_cycles", total / 32);
    }
    REPORT("khot.failures", fails);
    kos_printf("khot: %s\n", fails ? "FAIL" : "ok");
    return fails;
}
KOS_APP("khot", khot);
