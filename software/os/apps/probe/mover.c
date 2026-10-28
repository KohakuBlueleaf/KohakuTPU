/* mover [words]: the node's mover through SYS_MOVE.
 *   local   private pages -> private pages on this node, checked word for word
 *   land    this node's pages -> every other node's landing zone; the
 *           receiver, told over a channel, checks the words where they landed
 * A pattern per destination, so a copy that lands at the wrong node or offset
 * fails. Reports cycles per copy and bytes per kcycle. */
#include "../apps.h"

#define CH_LANDED 95
#define CH_CHECKED 96

static uint64_t pattern(uint64_t node, uint64_t i) { return ((node + 1) << 56) ^ ((i + 1) * 0x9E3779B97F4A7C15UL); }

/* on the receiving node: wait for "landed at off, words", check, answer */
static int64_t receiver(uint64_t unused)
{
    (void)unused;
    uint64_t m[2];
    unsigned src;
    if (kos_recv(CH_LANDED, m, 0, &src) != 16) {
        return 1;
    }
    volatile uint64_t *land = (volatile uint64_t *)((uint8_t *)kos_land() + m[0]);
    uint64_t bad = 0, me = kos_node();
    for (uint64_t i = 0; i < m[1] * 4; ++i) {
        bad += land[i] != pattern(me, i);
    }
    kos_send(src, CH_CHECKED, &bad, 8);
    return 0;
}

static int mover(int argc, char **argv)
{
    int fails = 0;
    uint64_t words = arg_u(argc, argv, 1, 64);
    if (words * 32 > KOS_LAND_SIZE / 2) {
        words = KOS_LAND_SIZE / 64;
    }
    unsigned me = kos_node(), n = kos_nodes();
    uint64_t *src = kos_mmap(words * 32), *dst = kos_mmap(words * 32);
    CHECK(fails, src && dst, "mover: mmap");
    if (!src || !dst) {
        return fails;
    }
    for (uint64_t i = 0; i < words * 4; ++i) {
        src[i] = pattern(me, i);
        dst[i] = ~0UL;
    }
    uint64_t t = kos_time();
    int64_t r = kos_move(src, (uint64_t)dst, words, 0);
    t = kos_time() - t;
    uint64_t bad = 0;
    for (uint64_t i = 0; i < words * 4; ++i) {
        bad += dst[i] != src[i];
    }
    CHECK(fails, r == 0 && bad == 0, "mover: local copy status %ld, %lu of %lu words differ", r, bad, words * 4);
    REPORT("mover.local_cycles", t);
    REPORT("mover.local_bytes_per_kcycle", t ? words * 32 * 1000 / t : 0);

    for (unsigned j = 0; j < n; ++j) {
        if (j == me) {
            continue;
        }
        int64_t rt = kos_spawn(receiver, 0, j);
        for (uint64_t i = 0; i < words * 4; ++i) {
            src[i] = pattern(j, i);
        }
        uint64_t off = 0x1000;
        t = kos_time();
        r = kos_move(src, off, words, KOS_MV_LAND(j));
        t = kos_time() - t;
        uint64_t m[2] = {off, words}, nbad = ~0UL;
        unsigned from;
        kos_send(j, CH_LANDED, m, 16);
        int got = kos_recv(CH_CHECKED, &nbad, 0, &from);
        int64_t code = -1;
        kos_join(rt, &code);
        CHECK(fails, r == 0 && got == 8 && nbad == 0 && code == 0,
              "mover: to node %u's landing zone: status %ld, %lu words wrong there, receiver exit %ld", j, r, nbad,
              code);
        if (j == (me + 1) % n) {
            REPORT("mover.land_cycles", t);
        }
    }
    kos_printf("mover: %lu words, %u nodes, %s\n", words, n, fails ? "FAIL" : "ok");
    return fails;
}

KOS_APP("mover", mover);
