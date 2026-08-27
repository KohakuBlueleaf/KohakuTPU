/* Minimal reproduction for bug #2: a console store issued from DRAM-resident
 * code, each store followed by a DRAM load. The memory note says the console
 * write (dbg_console_we = ctrl_wr && ctrl_off==R_CONSOLE) mis-times under the
 * I-cache stalls a following DRAM load creates, so bytes go missing -- while
 * R_EXIT (a store with no following load) is fine. Expected console: "ABCDEFGH".
 */

#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))

/* In DRAM: a console store, then a DRAM load, repeated -- the two share the one
 * node port and the store's ctrl-write detect must survive the load's stall. */
static void __attribute__((section(".dram_text"), noinline))
emit(volatile unsigned long *p)
{
    unsigned long chk = 0;
    unsigned i;
    for (i = 0; i < 8; ++i) {
        *R_CONSOLE = (unsigned char)('A' + i);   /* console store from DRAM */
        chk += p[i];                             /* FOLLOWING DRAM load     */
    }
    *R_CONSOLE = (unsigned char)'\n';
    p[8] = chk;                                  /* observable side effect  */
}

static void (*volatile emitp)(volatile unsigned long *) = emit;

int main(void)
{
    volatile unsigned long *p = (volatile unsigned long *)0x80004000UL;
    unsigned i;
    for (i = 0; i < 8; ++i) p[i] = i + 1;        /* 1..8, sum = 36 */
    emitp(p);
    *R_EXIT = (p[8] == 36) ? 0 : 1;              /* 0 iff the loads all landed */
    for (;;) { }
}
