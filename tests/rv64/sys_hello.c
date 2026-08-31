/* The SysCore program: proves the unit reaches the NODE ADDRESS SPACE.
 *
 * This is the thing `rv64_sys_pe` cannot do. A compute unit's loads and stores
 * land in its own spad and nowhere else; SysCore's decode sends anything at or
 * above NODE_BASE out the fabric port, so DRAM and staging L2 are ordinary
 * memory to it. Everything below still lands locally, and this checks both --
 * a decode that sent local traffic out the port would still "work" against a
 * memory model, so the local checks have to pass too. */

#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))

/* Above NODE_BASE, through the UNCACHED ALIAS (bit 38 set: the port sees the
 * address below it, no L1 in the way). Nothing is linked here; the fabric
 * answers it. NODE_SLICE moves this program's whole footprint so two units
 * sharing one fabric can be checked for NON-INTERFERENCE, not just for
 * running at once. */
#ifndef NODE_SLICE
#define NODE_SLICE 0
#endif
#define UNC       0x4000000000UL
#define NODE      ((volatile unsigned long *)(UNC + 0x10000000UL + (NODE_SLICE)))
#define NODE_FAR  ((volatile unsigned long *)(UNC + 0x18000000UL + (NODE_SLICE)))

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_u64(unsigned long v) {
    char b[21]; int n = 0;
    if (!v) { putch('0'); return; }
    while (v) { b[n++] = (char)('0' + (v % 10)); v /= 10; }
    while (n) putch(b[--n]);
}

static unsigned long local[32];
static int fails;

static void bad(const char *what, unsigned long got, unsigned long want)
{
    ++fails;
    put_str("FAIL "); put_str(what);
    put_str(" got "); put_u64(got);
    put_str(" want "); put_u64(want);
    putch('\n');
}

int main(void)
{
    unsigned long i;

    /* --- local still works -------------------------------------------- */
    for (i = 0; i < 32; ++i) local[i] = i * 7 + 1;
    for (i = 0; i < 32; ++i)
        if (local[i] != i * 7 + 1) { bad("spad", local[i], i * 7 + 1); break; }

    /* --- the node range: write then read back ------------------------- */
    for (i = 0; i < 16; ++i) NODE[i] = 0xC0FFEE00UL + i;
    for (i = 0; i < 16; ++i)
        if (NODE[i] != 0xC0FFEE00UL + i) {
            bad("node rw", NODE[i], 0xC0FFEE00UL + i);
            break;
        }

    /* A second region far from the first, so a decode that ignores the high
     * bits and aliases everything onto one beat is caught. */
    for (i = 0; i < 16; ++i) NODE_FAR[i] = 0x1234000UL + i;
    for (i = 0; i < 16; ++i)
        if (NODE_FAR[i] != 0x1234000UL + i) {
            bad("node far", NODE_FAR[i], 0x1234000UL + i);
            break;
        }
    /* ...and the first region must be untouched by the second. */
    for (i = 0; i < 16; ++i)
        if (NODE[i] != 0xC0FFEE00UL + i) { bad("node alias", NODE[i], 0xC0FFEE00UL + i); break; }

    /* --- byte and word granularity across the fabric ------------------ */
    volatile unsigned char *nb = (volatile unsigned char *)NODE;
    nb[0] = 0x11; nb[7] = 0x22; nb[9] = 0x33;
    if (nb[0] != 0x11 || nb[7] != 0x22 || nb[9] != 0x33)
        bad("node bytes", nb[0], 0x11);

    volatile unsigned int *nw = (volatile unsigned int *)(NODE + 4);
    nw[0] = 0xDEADBEEFU; nw[1] = 0x600DF00DU;
    if (nw[0] != 0xDEADBEEFU) bad("node word", nw[0], 0xDEADBEEFU);
    if (nw[1] != 0x600DF00DU) bad("node word2", nw[1], 0x600DF00DU);

    /* --- an atomic to the node range ---------------------------------- */
    /* SysCore keeps the A group precisely because staging L2 is single-reader
     * and cannot express a shared counter any other way. */
    NODE[20] = 100;
    unsigned long prev;
    __asm__ volatile ("amoadd.d %0, %2, (%1)"
                      : "=r"(prev) : "r"(&NODE[20]), "r"(5UL) : "memory");
    if (prev != 100) bad("amoadd ret", prev, 100);
    if (NODE[20] != 105) bad("amoadd mem", NODE[20], 105);

    /* --- the cached range, and it must survive eviction ------------------ */
    /* The L1 is 64 lines x 32 B = 2 KB. Touching 8 KB forces every line out
     * and back, so this checks the writeback path, not just the fill. */
    volatile unsigned long *dram =
        (volatile unsigned long *)(0x80000000UL + (NODE_SLICE));
    for (i = 0; i < 1024; ++i) dram[i] = i * 0x1000003UL + 7;
    for (i = 0; i < 1024; ++i)
        if (dram[i] != i * 0x1000003UL + 7) {
            bad("dram rw", dram[i], i * 0x1000003UL + 7);
            break;
        }

    /* Re-touch the first line: it was evicted long ago and has to come back
     * with the value the writeback carried out. */
    dram[0] += 1;
    if (dram[0] != 8) bad("dram refill", dram[0], 8);

    /* A byte store into a cached line, then a full-word read back. */
    volatile unsigned char *db = (volatile unsigned char *)&dram[4];
    db[0] = 0x5A;
    if (db[0] != 0x5A) bad("dram byte", db[0], 0x5A);

    if (!fails) put_str("syscore ok\n");
    *R_EXIT = fails;
    for (;;) { }
}
