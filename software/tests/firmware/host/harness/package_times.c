/* The package interpreter natively over the hardware double, each package named
 * on the command line run in turn, one line each: status, words sent, then the
 * timing words FIRST and LAST and the double's own first and last GO, all from
 * the step loop's start.
 *   package_times ENGINE FILE... */
#define _GNU_SOURCE
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>

#include <ka/boot/args.h>
#include <ka/hal/control.h>
#include <ka/lib/stdio.h>
#include <ka/os/task.h>
#include <ka/package/interp.h>
#include <ka/unit/unit.h>

#include "hw_double.h"

#define CARD 0x10000000UL
#define CARD_BYTES (1UL << 22)

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

int main(int argc, char **argv)
{
    dbl_engine = atoi(argv[1]);
    ka_boot.timeout = 1 << 22;
    void *card = mmap((void *)CARD, CARD_BYTES, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
    if (card != (void *)CARD) {
        return 2;
    }
    for (int k = 2; k < argc; ++k) {
        FILE *f = fopen(argv[k], "rb");
        size_t bytes = fread(card, 1, CARD_BYTES, f);
        fclose(f);
        dbl_gos = 0;
        struct ka_pkg_run run = {.pkg = CARD, .bytes = bytes};
        struct ka_pkg_result out;
        uint64_t before = dbl_cycles();
        ka_package_run(&run, &out);
        uint64_t *t = out.time;
        uint64_t loop = before + 1 + t[KA_PT_HEAD] + t[KA_PT_COPY] + t[KA_PT_BIND];
        printf("%d %" PRIu32 " %" PRIu64 " %" PRIu64 " %" PRId64 " %" PRId64 " %" PRIu64 "\n",
               out.status, out.sent, t[KA_PT_FIRST], t[KA_PT_LAST],
               dbl_gos ? (int64_t)(dbl_first_go - loop) : 0,
               dbl_gos ? (int64_t)(dbl_last_go - loop) : 0, dbl_gos);
    }
    return 0;
}
