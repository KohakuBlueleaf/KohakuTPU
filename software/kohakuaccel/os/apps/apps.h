/* Shared helpers of the apps: measurements, checks, workers over nodes. */
#ifndef KOS_APPS_H
#define KOS_APPS_H

#include <kos/user.h>

/* "@ name value": a measurement the host collects. */
#define REPORT(name, v) kos_printf("@ %s %lu\n", name, (uint64_t)(v))

/* Count a broken expectation and say what it was. */
#define CHECK(fails, cond, ...)                 \
    do {                                        \
        if (!(cond)) {                          \
            ++(fails);                          \
            kos_printf("FAIL " __VA_ARGS__);    \
            kos_printf("\n");                   \
        }                                       \
    } while (0)

static inline uint64_t arg_u(int argc, char **argv, int i, uint64_t dflt)
{
    if (i >= argc) {
        return dflt;
    }
    const char *p = argv[i];
    return kos_strtoul(&p);
}

/* Spawn `n` workers fn(i), worker i on node i % nodes; join them all; the
 * number that did not exit 0. */
static inline int run_workers(int64_t (*fn)(uint64_t), unsigned n)
{
    int64_t tid[64];
    unsigned nodes = kos_nodes();
    int bad = 0;
    for (unsigned i = 0; i < n && i < 64; ++i) {
        tid[i] = kos_spawn(fn, i, i % nodes);
        if (tid[i] < 0) {
            kos_printf("FAIL spawn worker %u: %ld\n", i, tid[i]);
            ++bad;
        }
    }
    for (unsigned i = 0; i < n && i < 64; ++i) {
        int64_t code = 0;
        if (tid[i] >= 0 && (kos_join(tid[i], &code) || code)) {
            kos_printf("FAIL worker %u exit %ld\n", i, code);
            ++bad;
        }
    }
    return bad;
}

static inline uint64_t value_at(uint64_t i) { return (i * 2654435761ULL) % 1000003ULL; }

/* Worker i's share [lo, hi) of n 8-byte elements, its ends on 64-byte
 * boundaries: L1s are written back a whole line at a time and are not
 * coherent, so two nodes must never write the same line in one epoch. */
static inline uint64_t chunk(uint64_t n, uint64_t w, uint64_t i)
{
    return i >= w ? n : (n / 8 * i / w) * 8;
}

#endif
