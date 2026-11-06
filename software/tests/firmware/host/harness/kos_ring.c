/* Ring egress with a held or advancing sent frontier. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

#define hw_time real_hw_time
#include "../../../../kohakuaccel/os/kernel/kernel.h"
#undef hw_time
#define hw_time() dbl_cycles()

static uint64_t ticks;
static unsigned writes, sent, advancing;

uint64_t dbl_cycles(void) { return ticks++; }
uint64_t dbl_rd(unsigned off)
{
    (void)off;
    if (advancing) {
        sent = writes;
    }
    return (uint64_t)(sent & 0xffffu) << 32;
}
void dbl_wr(unsigned off, uint64_t value)
{
    (void)value;
    if (off == HW_R_IL + HW_IL_RING) {
        ++writes;
    }
}

#define hw_sig_word() dbl_rd(HW_R_SIG)
#include APP_SOURCE

void k_where_wait(uint64_t code, uint64_t a, uint64_t b)
{
    (void)code;
    (void)a;
    (void)b;
}
void k_where_back(void) {}
void k_panic(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    printf("\nwrites %u\n", writes);
    exit(0);
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        return 2;
    }
    if (argv[1][0] == 'p') {
        advancing = 1;
        for (unsigned i = 0; i < 65540; ++i) {
            k_ring_word(1);
        }
        k_rings_drain();
        printf("progress %u\n", writes);
    } else if (argv[1][0] == 'd') {
        k_ring_word(1);
        k_rings_drain();
        return 3;
    } else {
        for (unsigned i = 0; i <= HW_RING_DEPTH; ++i) {
            k_ring_word(1);
        }
        return 4;
    }
    return 0;
}
