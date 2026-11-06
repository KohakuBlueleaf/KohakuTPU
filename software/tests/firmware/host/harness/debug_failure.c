/* Failure records retain the first issue count until the next begin. */
#define KA_DEBUG 1
#define KA_DEBUG_LAST 4 /* the debug images' value (firmware build.py) */
#include <stdio.h>
#include <ka/boot/args.h>
#include <ka/dispatch/de.h>
#include <ka/hal/mem.h>

static uint64_t memory[4096], stat, mover;
static uint64_t debug_rd(unsigned off)
{
    return off == KA_R_DE_STAT ? stat : off == KA_R_MVSTAT ? mover : 0;
}
static uint64_t debug_load(uint64_t a) { return memory[a / 8]; }
static void debug_store(uint64_t a, uint64_t v) { memory[a / 8] = v; }
#define ka_ctrl_rd debug_rd
#define ka_ld64 debug_load
#define ka_st64 debug_store
#include APP_SOURCE

volatile struct ka_bootargs ka_boot;
uint64_t ka_boot_region(uint64_t use, uint64_t *size)
{
    (void)use;
    *size = sizeof memory - 64;
    return 64;
}

int main(void)
{
    struct ka_engine e = {0};
    ka_dbg_boot();
    ka_dbg_begin(1, 0);
    stat = 37UL << 16;
    ka_dbg_failed();
    stat = 0;
    ka_dbg_failed();
    ka_dbg_fail(0, 0, 1, &e);
    printf("%llu\n", (unsigned long long)debug_load(D.base + D.rec + 56));
    ka_dbg_begin(2, 0);
    stat = 9UL << 16;
    ka_dbg_failed();
    ka_dbg_fail(0, 0, 1, &e);
    printf("%llu\n", (unsigned long long)debug_load(D.base + D.rec + 64 + 56));
    ka_dbg_begin(3, 0);
    mover = 1UL << 32;
    ka_dbg_fail(0, 0, 1, &e);
    printf("%llu\n", (unsigned long long)
           ((debug_load(D.base + D.rec + 128) >> 8) & KA_DBG_FAIL_DRAINED));
    printf("%llu\n", (unsigned long long)debug_load(D.base + D.rec + 128 + 56));
    return 0;
}
