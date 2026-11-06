/* Lists of packages over the hardware double, each run as the queue service
 * runs a list: chained when CHAIN is set, stopped at a failure, the chain ended
 * after it. Lists are parted by "/". One line per package, status and the
 * retirements still to come when it returned; one per list, status, then clear
 * writes, peak words a unit held, GOs, retirements still to come, rings sent and
 * rings dropped.
 *   package_chain ENGINE CHAIN RETIRE LOST EXTRA FILE... [/ FILE...]...
 * LOST of the list's first GOs never retire; with EXTRA, one retirement nobody
 * asked for follows the first package. */
#define _GNU_SOURCE
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

#include <ka/boot/args.h>
#include <ka/hal/control.h>
#include <ka/hal/cpu.h>
#include <ka/lib/stdio.h>
#include <ka/os/task.h>
#include <ka/package/interp.h>
#include <ka/unit/unit.h>

#include "hw_double.h"

#define CARD 0x10000000UL
#define CARD_BYTES (1UL << 22)
#define SLOT (1UL << 18)
#define MAX_LIST 16

volatile struct ka_bootargs ka_boot;

void ka_yield(void) { dbl_cycles(); }

int ka_printf(const char *fmt, ...)
{
    (void)fmt;
    return 0;
}

enum ka_verdict ka_unit_generic_classify(unsigned code, uint32_t arg)
{
    (void)arg;
    return code == KA_SIG_FAULT ? KA_V_FAULT : code == KA_SIG_DATA_RECEIVED ? KA_V_ACK : KA_V_DONE;
}

static const struct ka_unit_class generic = {.classify = ka_unit_generic_classify};

const struct ka_unit_class *ka_unit_class(uint16_t type)
{
    (void)type;
    return &generic;
}

static unsigned fatal_rings0;
void ka_on_fatal(uint64_t code, uint64_t a, uint64_t b)
{
    (void)a; (void)b;
    printf("L %u %u %u %" PRIu64 " %u %u %u\n", (unsigned)code,
           dbl_clears, dbl_peak, dbl_gos, dbl_pending(),
           dbl_rings - fatal_rings0, dbl_ring_drops);
}
void ka_exit(uint64_t code)
{
    (void)code;
    exit(0);
}

static void run_list(char **files, int n, int chain, unsigned lost, int extra)
{
    size_t bytes[MAX_LIST];
    for (int i = 0; i < n; ++i) {
        FILE *f = fopen(files[i], "rb");
        bytes[i] = fread((char *)CARD + i * SLOT, 1, SLOT, f);
        fclose(f);
    }
    dbl_gos = dbl_peak = dbl_clears = dbl_ring_drops = 0;
    unsigned rings0 = dbl_rings;
    fatal_rings0 = rings0;
    dbl_lost = lost;
    int status = 0;
    for (int i = 0; i < n && !status; ++i) {
        struct ka_pkg_run run = {
            .pkg = CARD + i * SLOT, .bytes = bytes[i], .chain = chain && i + 1 < n,
        };
        struct ka_pkg_result out;
        ka_package_run(&run, &out);
        status = out.status;
        printf("P %d %u\n", status, dbl_pending());
        if (extra && !i) {
            dbl_peer_retires(1, 2);
        }
    }
    ka_package_unchain();
    for (uint64_t t0 = dbl_cycles(); dbl_cycles() - t0 < 1024;) {
    }
    printf("L %d %u %u %" PRIu64 " %u %u %u\n", status, dbl_clears, dbl_peak, dbl_gos,
           dbl_pending(), dbl_rings - rings0, dbl_ring_drops);
}

int main(int argc, char **argv)
{
    dbl_engine = atoi(argv[1]);
    int chain = atoi(argv[2]);
    dbl_retire = (unsigned)atoi(argv[3]);
    ka_boot.timeout = 1 << 16;
    void *card = mmap((void *)CARD, CARD_BYTES, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
    if (card != (void *)CARD) {
        return 2;
    }
    unsigned lost = (unsigned)atoi(argv[4]);
    int extra = atoi(argv[5]);
    for (int k = 6, first = 6; k <= argc; ++k) {
        if (k == argc || !strcmp(argv[k], "/")) {
            run_list(argv + first, k - first, chain, lost, extra);
            first = k + 1;
            lost = 0;
            extra = 0;
        }
    }
    return 0;
}
