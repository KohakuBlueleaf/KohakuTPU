/* Milestone 2: the RV64 reaches DRAM across the real MAG -> dram_ -> axi_ram
 * path on the mesh. Writes a pattern larger than the L1 (64 lines x 32 B = 2 KB)
 * so every line is evicted and refilled -- this exercises the writeback, not
 * just the fill -- then reads it back and re-touches line 0. */

#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_u64(unsigned long v) {
    char b[21]; int n = 0;
    if (!v) { putch('0'); return; }
    while (v) { b[n++] = (char)('0' + (v % 10)); v /= 10; }
    while (n) { putch(b[--n]); }
}

int main(void)
{
    volatile unsigned long *dram = (volatile unsigned long *)0x80000000UL;
    unsigned long i;
    int fails = 0;

    for (i = 0; i < 1024; ++i) { dram[i] = i * 0x1000003UL + 7; }
    for (i = 0; i < 1024; ++i) {
        if (dram[i] != i * 0x1000003UL + 7) {
            ++fails;
            put_str("FAIL dram["); put_u64(i); put_str("]=");
            put_u64(dram[i]); putch('\n');
            break;
        }
    }
    dram[0] += 1;
    if (dram[0] != 8) { ++fails; put_str("FAIL refill\n"); }

    if (!fails) { put_str("dram ok\n"); }
    *R_EXIT = fails;
    for (;;) { }
}
