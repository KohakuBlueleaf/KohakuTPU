/* Mesh 1 of rv64_node_pair: the consumer. Waits on each of sig_a.c's slots,
 * checks what landed, acks on mesh 0's slot 2. */

#include "sig.h"

#define WORDS   32
#define OFF2    0x1000UL
#define OFF3    0x2000UL
#define OFF4    0x3000UL
#define OFF5    0x4000UL
#define PINGS   16
#define INIT    0xEEEEEEEEEEEEEEEEUL

static void ack(void) { ring(0, 2, 1, 0); }

static void check_words(const char *what, unsigned long off, unsigned long seed,
                        unsigned long n64)
{
    volatile unsigned long *p = (volatile unsigned long *)(STG(1) + off);
    unsigned long bad = 0;
    for (unsigned long i = 0; i < n64; ++i) bad += (p[i] != pattern(i, seed));
    check(what, bad, 0UL);
}

int main(void)
{
    /* the word phase 4 writes half of, so a lost strobe shows */
    volatile unsigned long *r = (volatile unsigned long *)(STG(1) + OFF4);
    for (int i = 0; i < 8; ++i) r[i] = INIT;

    /* 1 */
    check("slot 3 by 5", wait_slot(3, 5), 5UL);
    check("slot 7 by 1", wait_slot(7, 1), 1UL);
    consume(3, 5);
    select_slot(3);
    unsigned long g = 0;
    while ((sig_word() & 0xffffffffUL) != 0 && ++g < 64) { }
    check("slot 3 consumed", sig_word() & 0xffffffffUL, 0UL);
    ack();

    /* 2, 3 */
    wait_slot(4, 1);
    check_words("fenced copy landed", OFF2, 2, WORDS * 4);
    ack();
    wait_slot(5, 1);
    check_words("polled copy landed", OFF3, 3, WORDS * 4);
    ack();

    /* 4 */
    wait_slot(6, 1);
    for (unsigned long i = 0; i < 4; ++i) check("remote store word", r[i], pattern(i, 4));
    check("lane 0", r[4], pattern(4, 4));
    check("lane 1 kept", r[5], INIT);
    check("lane 2", r[6], pattern(6, 4));
    check("lane 3 kept", r[7], INIT);
    ack();

    /* 5 */
    wait_slot(8, 1);
    check_words("mover message landed", OFF5, 5, 4);
    ack();

    /* 6 */
    for (unsigned long k = 1; k <= PINGS; ++k) {
        wait_slot(9, k);
        ring(0, 10, 1, 0);
    }

    check("no interlink fault", sig_word() >> 56, 0UL);
    if (fails == 0) put_str("sig b ok\n");
    return fails;
}
