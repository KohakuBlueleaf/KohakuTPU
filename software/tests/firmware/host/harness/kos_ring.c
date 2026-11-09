/* Ring egress with a held or advancing sent frontier. */
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

#define hw_time real_hw_time
#include "../../../../kohakuaccel/os/kernel/kernel.h"
#undef hw_time
#define hw_time() dbl_cycles()

static uint64_t ticks;
static unsigned writes, sent, advancing, tokens;
struct knode kn;
static uint32_t slot_count(unsigned slot)
{
    assert(slot == KOS_SLOT_USER);
    return tokens;
}

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
    if (off == HW_R_IL + HW_IL_CONSUME) {
        assert((value & 0xffffffffu) == KOS_SLOT_USER);
        tokens -= (unsigned)(value >> 32);
    }
    if (off == HW_R_IL + HW_IL_RING) {
        ++writes;
    }
}

#define hw_slot(slot) slot_count(slot)
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
    if (argv[1][0] == 's') {
        kn.n = 1;
        tokens = 1;
        assert(k_slot_try(0, 0, KOS_SLOT_USER, 2) == KOS_EAGAIN);
        assert(tokens == 1);
        tokens = 2;
        assert(k_slot_try(0, 0, KOS_SLOT_USER, 2) == KOS_OK);
        assert(tokens == 0);
        for (unsigned slot = 0; slot < KOS_SLOT_USER; ++slot) {
            for (unsigned release = 0; release < 2; ++release) {
                assert(k_slot_try(release, 0, slot, 1) == KOS_EINVAL);
                assert(tokens == 0 && writes == 0);
            }
        }
        for (unsigned i = 0; i < HW_RING_DEPTH; ++i) {
            assert(k_slot_try(1, 0, KOS_SLOT_USER, 1) == KOS_OK);
        }
        assert(k_slot_try(1, 0, KOS_SLOT_USER, 1) == KOS_EAGAIN);
        assert(writes == HW_RING_DEPTH);
        sent = 1;
        assert(k_slot_try(1, 0, KOS_SLOT_USER, 1) == KOS_OK);
        assert(writes == HW_RING_DEPTH + 1);
    } else if (argv[1][0] == 'p') {
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
