/* `mg` natively, as the image builds it, over a double of its system calls: UNITS
 * MG units, each round's words run at once, a DRAIN writing its one word. The
 * landing zone is mapped as the kernel maps it, an unmapped page behind it, so a
 * store past it dies here as it faults there.
 *   mg_app UNITS -> mg's return value */
#define _GNU_SOURCE
#define KOS_REMOTE_BATCH
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>

#include APP_SOURCE

#define LAND 0x30000000UL
#define PAGE 4096UL
#define WORDS 4u /* 8-byte words a DRAIN of n = 1 writes */

static unsigned units;
static uint64_t retired_of[KOS_MAX_UNITS], tickets, clock_now;

int kos_printf(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = vprintf(fmt, ap);
    va_end(ap);
    return n;
}

uint64_t kos_strtoul(const char **p)
{
    (void)p;
    abort();
}

uint64_t kos_time(void) { return ++clock_now; }
volatile void *kos_land(void) { return (volatile void *)LAND; }

uint64_t kos_info(unsigned key)
{
    return key == KOS_I_LANDPA ? LAND : key == KOS_I_FLAGS ? KOS_F_ENGINE : 0;
}

int64_t kos_units(struct kos_unit *buf, unsigned max)
{
    for (unsigned i = 0; i < units && i < max; ++i) {
        buf[i] = (struct kos_unit){.type = 0x4D47, .mesh = (uint8_t)(i / 4), .x = 1, .y = 1};
    }
    return units;
}

/* A word's opcode, bits 255:252, and its address: bits 251:218, then 68:63. */
static void word(unsigned u, const uint64_t w[4])
{
    if (w[3] >> 60 == DRAIN) {
        uint64_t hi = (w[0] >> 63) | ((w[1] & 31) << 1);
        uint64_t at = ((w[3] >> 26) & ((1UL << 34) - 1)) | hi << 34;
        for (unsigned i = 0; i < WORDS; ++i) {
            ((volatile uint64_t *)at)[i] = 0;
        }
    }
    ++retired_of[u];
}

int64_t kos_dispatch(unsigned unit, const uint64_t w[4])
{
    word(unit, w);
    return (int64_t)++tickets;
}

int64_t kos_engine(unsigned unit, const uint64_t *w, unsigned count)
{
    for (unsigned k = 0; k < count; ++k) {
        kos_dispatch(unit, &w[4 * k]);
    }
    return (int64_t)tickets;
}

int kos_unit_wait(unsigned unit, uint64_t ticket, uint64_t timeout, uint64_t *faults)
{
    (void)unit;
    (void)ticket;
    (void)timeout;
    *faults = 0;
    return 0;
}

int kos_unit_ctrl(unsigned unit, unsigned index, uint64_t *value)
{
    (void)index;
    *value = retired_of[unit] << 32;
    return 0;
}

int main(int argc, char **argv)
{
    units = (unsigned)atoi(argv[1]);
    (void)argc;
    uint64_t bytes = KOS_USER_LAND_SIZE;
    void *land = mmap((void *)LAND, bytes + PAGE, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
    if (land != (void *)LAND || mprotect((char *)land + bytes, PAGE, PROT_NONE)) {
        return 2;
    }
    char *args[] = {"mg", 0};
    printf("rc %d\n", mg(1, args));
    return 0;
}
