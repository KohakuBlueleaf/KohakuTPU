/* Minimal reproduction: a function resident in DRAM that does DRAM data stores
 * while it executes. That makes I-cache fills (fetch) and L1 traffic (data) both
 * want the one node port at once -- the overlap rv64_nport's header says it does
 * not handle. If the port cross-routes or corrupts, the loop faults or the check
 * fails. */

#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) { putch(*s++); } }

/* In DRAM: executes from DRAM (I-cache fills) and stores to DRAM (L1). */
static void __attribute__((section(".dram_text"), noinline)) work(unsigned long *p)
{
    unsigned i;
    for (i = 0; i < 64; ++i) { p[i] = i * 7 + 1; }
}

/* File-scope volatile pointer: an absolute (R_RISCV_64) init, so the imem->DRAM
 * call is a jalr, not a truncated PC-relative call. */
static void (*volatile workp)(unsigned long *) = work;

int main(void)
{
    volatile unsigned long *p = (volatile unsigned long *)0x80004000UL;
    unsigned i;
    int fails = 0;
    workp((unsigned long *)p);
    for (i = 0; i < 64; ++i) {
        if (p[i] != i * 7 + 1) { ++fails; break; }
    }
    if (!fails) { put_str("conc ok\n"); }
    *R_EXIT = fails;
    for (;;) { }
}
