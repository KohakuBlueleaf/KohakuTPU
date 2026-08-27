/* fence_demo.c -- prove FENCE.I invalidates the I-cache.
 *
 * funcA lives in DRAM and is called once, so the I-cache holds its line. Then
 * the host is asked (over an uncached mailbox) to overwrite funcA's bytes in
 * DRAM with funcB's. Calling funcA again WITHOUT fence.i still runs the cached
 * old code -- that is the stale read the cache must be able to drop. After
 * fence.i the line is gone, the next call refills from DRAM, and the NEW code
 * runs. The three results together are the proof.
 */

#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))

/* An uncached mailbox in the node range (bit 31 clear -> uncached), so writes
 * reach the host immediately and it can act on them. */
#define HS 0x10001000UL
#define H_SRC ((volatile unsigned long *)(HS + 0x00))
#define H_DST ((volatile unsigned long *)(HS + 0x08))
#define H_LEN ((volatile unsigned long *)(HS + 0x10))
#define H_GO  ((volatile unsigned long *)(HS + 0x18))
#define H_ACK ((volatile unsigned long *)(HS + 0x20))

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_u64(unsigned long v)
{
    char b[21]; int n = 0;
    if (!v) { putch('0'); return; }
    while (v) { b[n++] = (char)('0' + v % 10); v /= 10; }
    while (n) putch(b[--n]);
}

__attribute__((section(".dram_text"), noinline, used))
int funcA(int a, int b) { return a + b + 0x5A0000; }
__attribute__((section(".dram_text"), noinline, used))
int funcB(int a, int b) { return a + b + 0x0B0000; }

static int (*volatile fp)(int, int) = funcA;
static unsigned long volatile addr_a = (unsigned long)funcA;
static unsigned long volatile addr_b = (unsigned long)funcB;

int main(void)
{
    put_str("--- FENCE.I demo: code in DRAM changes under the I-cache ---\n");

    int r1 = fp(10, 20);
    put_str("call 1 (funcA)            : "); put_u64((unsigned long)r1); putch('\n');

    /* ask the host to copy funcB's bytes over funcA's, in DRAM */
    *H_SRC = addr_b;
    *H_DST = addr_a;
    *H_LEN = 32;
    *H_GO  = 1;
    while (*H_ACK == 0) { }

    int stale = fp(10, 20);      /* still the cached funcA */
    put_str("call 2, no fence (stale)  : "); put_u64((unsigned long)stale); putch('\n');

    __asm__ volatile ("fence.i" ::: "memory");

    int fresh = fp(10, 20);      /* refilled: now funcB */
    put_str("call 3, after fence.i     : "); put_u64((unsigned long)fresh); putch('\n');

    int ok = (r1 == 10 + 20 + 0x5A0000)
          && (stale == r1)
          && (fresh == 10 + 20 + 0x0B0000);
    if (ok) put_str("fencei ok\n");

    *R_EXIT = ok ? 0 : 1;
    for (;;) { }
}
