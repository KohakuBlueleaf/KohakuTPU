/* pipeline COUNT: a producer/consumer chain through channels, one stage per
 * node: stage 0 makes 1..COUNT, every later stage maps v -> 3v + k and
 * forwards, the last stage folds a checksum and sends it back to node 0.
 * Message passing only, so it runs on boards without shared memory too. */
#include "../apps.h"

#define CH_STAGE(k) (100 + (k))
#define CH_DONE 99

static int64_t stage(uint64_t k)
{
    unsigned n = kos_nodes();
    uint64_t count;
    unsigned src;
    if (kos_recv(CH_STAGE(k), &count, 0, &src) != 8) {
        return 1;
    }
    uint64_t sum = 0;
    for (uint64_t i = 1; i <= count; ++i) {
        uint64_t v;
        if (k == 0) {
            v = i;
        } else if (kos_recv(CH_STAGE(k), &v, 0, &src) != 8) {
            return 2;
        }
        v = v * 3 + k;
        if (k + 1 < n) {
            kos_send((unsigned)(k + 1), CH_STAGE(k + 1), &v, 8);
        } else {
            sum += v;
        }
    }
    if (k + 1 == n) {
        kos_send(0, CH_DONE, &sum, 8);
    }
    return 0;
}

static int pipeline(int argc, char **argv)
{
    int fails = 0;
    unsigned n = kos_nodes();
    uint64_t count = arg_u(argc, argv, 1, 64);
    int64_t tid[KOS_MAX_NODES];
    for (unsigned k = 0; k < n; ++k) {
        tid[k] = kos_spawn(stage, k, k);
    }
    uint64_t t0 = kos_time();
    for (unsigned k = 0; k < n; ++k) {
        kos_send(k, CH_STAGE(k), &count, 8);
    }
    uint64_t sum = 0;
    unsigned src;
    int got = kos_recv(CH_DONE, &sum, 0, &src);
    uint64_t t = kos_time() - t0;
    for (unsigned k = 0; k < n; ++k) {
        int64_t code = 0;
        if (tid[k] < 0 || kos_join(tid[k], &code) || code) {
            ++fails;
            kos_printf("FAIL pipeline stage %u: tid %ld exit %ld\n", k, tid[k], code);
        }
    }
    uint64_t want = 0;
    for (uint64_t i = 1; i <= count; ++i) {
        uint64_t v = i;
        for (unsigned k = 0; k < n; ++k) {
            v = v * 3 + k;
        }
        want += v;
    }
    CHECK(fails, got == 8 && sum == want, "pipeline: checksum %lx want %lx", sum, want);
    REPORT("pipeline.messages", count * (n > 1 ? n - 1 : 0));
    REPORT("pipeline.cycles", t);
    REPORT("pipeline.cycles_per_item", count ? t / count : 0);
    kos_printf("pipeline: %lu items through %u stages, %s\n", count, n, fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("pipeline", pipeline);
