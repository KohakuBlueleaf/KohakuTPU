/* icache_demo.c -- run code that lives in DRAM, through the I-cache.
 *
 * `dram_func` is placed in the .dram_text section, which the link map puts at
 * 0x8000_0000 (the cached node range). The harness loads that straight into node
 * memory, so the core cannot reach it through the on-chip window -- every fetch
 * of it is an I-cache access, a miss that fills from DRAM the first time and a
 * hit after. If the results are right, the I-cache fetched and ran DRAM code.
 *
 * The call is indirect through a file-scope pointer whose initialiser is an
 * absolute (R_RISCV_64) relocation, so the compiler never tries a PC-relative
 * reach across the 2 GB gap between the window and DRAM.
 */

#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))

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
int dram_func(int a, int b) { return a + b + 0x5A0000; }

/* File scope so the initialiser is an absolute relocation; volatile so the
 * optimiser cannot devirtualise the call back to a (PC-relative) direct one. */
static int (*volatile fp)(int, int) = dram_func;   /* resolves to 0x8000_0000 */

int main(void)
{
    put_str("running code that lives in DRAM, through the I-cache...\n");

    /* Exercise a fill then many hits: the same lines, called 256 times. */
    long acc = 0;
    for (int i = 0; i < 256; ++i) acc += fp(i & 7, i & 3);

    int r = fp(3, 4);
    put_str("dram_func(3,4) = "); put_u64((unsigned long)r); putch('\n');
    put_str("loop accumulator = "); put_u64((unsigned long)acc); putch('\n');

    int ok = (r == 3 + 4 + 0x5A0000);
    if (ok) put_str("icache ok\n");

    *R_EXIT = ok ? 0 : 1;
    for (;;) { }
}
