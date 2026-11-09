/* The kernel's unit code natively over the hardware double, one command a line on
 * stdin; unit 0 sits at (1, 2) with a credit of 4:
 *   s N P   a descriptor stream of N words to unit 0 and a WAIT for N + P, the
 *           double's peer retiring P more -> "st sent retired"
 *   d       one dispatch to unit 0 -> "ticket"
 *   w       wait for every completion in flight -> "sent retired"
 *   m       one k_move -> "st" and the window writes by side and axis
 *   c       the stream counters -> "streams entries open load room tail span moves
 *           move_cycles", span the double's own cycles across every stream */
#include <assert.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

#include "../../../../kohakuaccel/os/kernel/bootstrap.h"
#include "hw_double.h"

#define X 1u
#define Y 2u
#define MAX_ENTRIES 3000u

struct knode kn;
struct kos_boot k_boot;
struct kos_kernel_stat kstat;

static unsigned allocations;
static int index_visible;

void *k_alloc(uint64_t bytes)
{
    if (allocations) {
        assert(index_visible);
    }
    ++allocations;
    return calloc(1, bytes);
}

static void boot_cache(unsigned op)
{
    assert(allocations == 1);
    hw_dcache(op);
    index_visible = 1;
}
int k_pa_ok(uint64_t pa, uint64_t bytes)
{
    (void)pa;
    (void)bytes;
    return 1;
}
int k_copy_out(uint64_t uva, const void *src, uint64_t n)
{
    (void)uva;
    (void)src;
    (void)n;
    abort();
}
void k_ring_word(uint64_t value)
{
    (void)value;
    abort();
}

void k_reply(unsigned node, uint64_t req, int64_t status, uint64_t value)
{
    (void)node;
    (void)req;
    (void)status;
    (void)value;
    abort();
}

static uint64_t stream[4 * MAX_ENTRIES / 3 + 4];
static unsigned entries;

static void put(unsigned code, uint64_t v)
{
    uint64_t *group = &stream[4 * (entries / 3)];
    unsigned j = entries % 3;
    group[0] = (j ? group[0] : 0) | (uint64_t)code << (8 * j);
    group[j + 1] = v;
    ++entries;
}

static void words(unsigned n, unsigned peers)
{
    entries = 0;
    for (unsigned k = 0; k < n; ++k) {
        put(KOS_DE_MB_DST, (uint64_t)Y << 8 | X);
        for (unsigned a = 0; a < 4; ++a) {
            put(KOS_DE_MB_ARG0 + a, k + a);
        }
        put(KOS_DE_MB_GO, 1);
    }
    put(KOS_DE_WAIT_CODE, n + peers);
    for (unsigned p = 0; p < peers; ++p) {
        dbl_peer_retires(X, Y);
    }
}

int main(void)
{
    dbl_engine = 1;
    kn.engine = 1;
    k_boot.nunits = 1;
    k_boot.unit[0] = (struct kos_unit){.type = 0x4D47, .x = X, .y = Y, .depth = 4};
    const struct k_boot_kernel boot = {
        .node = &kn, .boot = &k_boot, .units = &k_boot_units,
        .alloc = k_alloc, .dcache = boot_cache, .poll_units = k_units_poll,
    };
    k_boot_units_init(&boot);
    uint64_t w[4] = {1, 2, 3, 4}, span = 0;
    char op;
    while (scanf(" %c", &op) == 1) {
        if (op == 'e') {
            assert(k_de_epoch(0, 0, KOS_DE_OPEN) == KOS_OK);
            unsigned clears = dbl_clears;
            dbl_retire = 1000;
            words(1, 0);
            --entries; /* no retirement frontier before the acquire */
            assert(k_de_epoch(stream, entries, KOS_DE_FEED) == KOS_OK);
            k_units_poll();
            assert(dbl_pending());
            dbl_peer_retires(X, Y);
            for (unsigned i = 0; i < 4096; ++i) {
                k_units_poll();
            }
            assert(k_unit_retired(0) == 2);
            entries = 0;
            put(KOS_DE_WAIT_CODE, 2);
            assert(k_de_epoch(stream, entries, KOS_DE_FEED) == KOS_OK);
            assert(dbl_clears == clears);
            int64_t st = KOS_EAGAIN;
            for (unsigned i = 0; i < 4096 && st == KOS_EAGAIN; ++i) {
                st = k_de_epoch(0, 0, KOS_DE_FINISH);
            }
            assert(st == KOS_OK && kunits[0].sent == 2 && k_unit_retired(0) == 2);
            puts("epoch retired local and incoming work");
        }
        else if (op == 's') {
            unsigned n, p;
            if (scanf("%u %u", &n, &p) != 2) {
                return 2;
            }
            words(n, p);
            uint64_t t0 = dbl_cycles();
            int64_t st = k_de_stream(stream, entries);
            span += dbl_cycles() - t0;
            printf("%" PRId64 " %" PRIu64 " %" PRIu64 "\n", st, kunits[0].sent, k_unit_retired(0));
        }
        else if (op == 'c') {
            printf("%" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64
                   " %" PRIu64 " %" PRIu64 "\n",
                   kstat.de_streams, kstat.de_entries, kstat.cyc_de_open, kstat.cyc_de_load,
                   kstat.cyc_de_room, kstat.cyc_de_tail, span, kstat.moves, kstat.cyc_move);
        }
        else if (op == 'd') {
            printf("%" PRId64 "\n", k_dispatch_local(0, w));
        }
        else if (op == 'w') {
            for (unsigned k = 0; k < 256; ++k) {
                k_units_poll();
            }
            printf("%" PRIu64 " %" PRIu64 "\n", kunits[0].sent, k_unit_retired(0));
        }
        else if (op == 'm') {
            int64_t st = k_move(0x1000, 0x2000, 4);
            printf("%" PRId64 " %u %u %u %u\n", st, dbl_windows[0], dbl_windows[1], dbl_windows[2],
                   dbl_windows[3]);
        }
    }
    return 0;
}
