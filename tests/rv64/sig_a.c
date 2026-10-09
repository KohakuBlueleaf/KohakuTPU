/* Mesh 0 of rv64_node_pair: the producer. Each phase hands something to mesh 1
 * and waits for its ack on slot 2; lines starting "@ " are cycle counts.
 *
 *   1  slots and amounts          ring slot 3 by 5, slot 7 by 0 (counts 1)
 *   2  fenced ring after a copy   mover copy, ring at once, no idle poll
 *   3  the same, today's way      poll the mover idle, then a plain ring
 *   4  posted remote stores       8-byte stores into mesh 1's staging, fenced ring
 *   5  the same, today's way      local staging, mover copy, idle poll, ring
 *   6  ping-pong                  signal latency, 16 round trips
 *   7  publishing 1 KB            uncached stores against cached + D-cache flush */

#include "sig.h"

#define WORDS   32                    /* 1 KB in 32-byte words */
#define OFF2    0x1000UL
#define OFF3    0x2000UL
#define OFF4    0x3000UL
#define OFF5    0x4000UL
#define PINGS   16

static unsigned long acks;

static void wait_ack(void)
{
    ++acks;
    check("ack", wait_slot(2, acks), acks);
}

static void fill_local(unsigned long off, unsigned long seed, unsigned long n64)
{
    volatile unsigned long *p = (volatile unsigned long *)(STG(0) + off);
    for (unsigned long i = 0; i < n64; ++i) p[i] = pattern(i, seed);
}

int main(void)
{
    unsigned long t0, t1, t2;

    /* 1 */
    check("slots swept", (sig_word() >> 49) & 1UL, 0UL);
    ring(1, 3, 5, 0);
    ring(1, 7, 0, 0);
    wait_ack();

    /* 2 */
    fill_local(OFF2, 2, WORDS * 4);
    t0 = cycles();
    mover_copy(STG(0) + OFF2, STG(1) + OFF2, WORDS);
    ring(1, 4, 1, 1);
    t1 = cycles();
    wait_ack();
    t2 = cycles();
    report("fenced.producer", t1 - t0);
    report("fenced.round_trip", t2 - t0);

    /* 3 */
    fill_local(OFF3, 3, WORDS * 4);
    t0 = cycles();
    mover_copy(STG(0) + OFF3, STG(1) + OFF3, WORDS);
    mover_idle();
    ring(1, 5, 1, 0);
    t1 = cycles();
    wait_ack();
    t2 = cycles();
    report("polled.producer", t1 - t0);
    report("polled.round_trip", t2 - t0);

    /* 4: one whole word, then lanes 0 and 2 of the next */
    volatile unsigned long *r = (volatile unsigned long *)(STG(1) + OFF4);
    t0 = cycles();
    for (unsigned long i = 0; i < 4; ++i) r[i] = pattern(i, 4);
    r[4] = pattern(4, 4);
    r[6] = pattern(6, 4);
    ring(1, 6, 1, 1);
    t1 = cycles();
    wait_ack();
    t2 = cycles();
    report("remote_store.producer", t1 - t0);
    report("remote_store.round_trip", t2 - t0);

    /* 5: the same four words through local staging and the mover */
    t0 = cycles();
    fill_local(OFF5, 5, 4);
    mover_copy(STG(0) + OFF5, STG(1) + OFF5, 1);
    mover_idle();
    ring(1, 8, 1, 0);
    t1 = cycles();
    wait_ack();
    t2 = cycles();
    report("mover_msg.producer", t1 - t0);
    report("mover_msg.round_trip", t2 - t0);

    /* 6 */
    t0 = cycles();
    for (unsigned long k = 1; k <= PINGS; ++k) {
        ring(1, 9, 1, 0);
        check("pong", wait_slot(10, k), k);
    }
    t1 = cycles();
    report("ping_pong.per_round_trip", (t1 - t0) / PINGS);

    /* 7: 1 KB published for another agent to read */
    volatile unsigned long *u = (volatile unsigned long *)(UNC | (DRAM + 0x100000UL));
    volatile unsigned long *c = (volatile unsigned long *)(DRAM + 0x140000UL);
    volatile unsigned long *cu = (volatile unsigned long *)(UNC | (DRAM + 0x140000UL));
    t0 = cycles();
    for (unsigned long i = 0; i < WORDS * 4; ++i) u[i] = pattern(i, 7);
    t1 = cycles();
    for (unsigned long i = 0; i < WORDS * 4; ++i) c[i] = pattern(i, 8);
    REG(DCACHE) = 1;
    unsigned long g = 0;
    while (REG(DCACHE) && ++g < 1000000) { }
    t2 = cycles();
    report("publish.uncached", t1 - t0);
    report("publish.cached_flush", t2 - t1);
    check("flushed first word", cu[0], pattern(0, 8));
    check("flushed last word", cu[WORDS * 4 - 1], pattern(WORDS * 4 - 1, 8));

    check("no interlink fault", sig_word() >> 56, 0UL);
    check("every ring sent", rings_sent(), rings_written & 0xffffUL);
    if (fails == 0) put_str("sig a ok\n");
    return fails;
}
