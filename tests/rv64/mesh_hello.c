/* Milestone 1 for the whole-mesh bench: the RV64 boots on the mesh, runs from
 * its own spad, and reaches the console. No DRAM, no dispatch -- this only
 * proves the mesh elaborates, clocks, resets, and the processor runs on it. */

#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }

static unsigned long local[64];

int main(void)
{
    unsigned long i;
    int fails = 0;
    for (i = 0; i < 64; ++i) { local[i] = i * 1315423911UL + 7; }
    for (i = 0; i < 64; ++i) {
        if (local[i] != i * 1315423911UL + 7) { ++fails; break; }
    }
    if (!fails) { put_str("mesh ok\n"); }
    *R_EXIT = fails;
    for (;;) { }
}
