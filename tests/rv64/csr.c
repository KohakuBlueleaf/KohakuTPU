/* Zicsr, trap entry and return, and the timer interrupt.
 *
 * This is the only program that exercises `rv64_csr` and the trap path. It
 * checks the three things the runtime depends on and nothing else: that the
 * counters run, that an exception reaches a handler and comes back, and that a
 * timer interrupt fires and can be dismissed.
 *
 * WITH NO HANDLER INSTALLED THE CORE HALTS INSTEAD OF TRAPPING, so `mtvec` must
 * be set before anything here can fault deliberately. */

#define CONSOLE ((volatile unsigned char *)0x10000000UL)

static void putch(char c) { *CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_u64(unsigned long v) {
    char b[21]; int n = 0;
    if (!v) { putch('0'); return; }
    while (v) { b[n++] = (char)('0' + (v % 10)); v /= 10; }
    while (n) putch(b[--n]);
}

#define csrr(name) ({ unsigned long v; \
    __asm__ volatile ("csrr %0, " name : "=r"(v)); v; })
#define csrw(name, v) \
    __asm__ volatile ("csrw " name ", %0" :: "r"((unsigned long)(v)))
#define csrs(name, v) \
    __asm__ volatile ("csrrs x0, " name ", %0" :: "r"((unsigned long)(v)))

#define MTIMECMP "0x7c0"
#define CAUSE_ILLEGAL 2UL
#define CAUSE_ECALL_M 11UL
#define CAUSE_TIMER   0x8000000000000007UL

volatile unsigned long trap_count;
volatile unsigned long last_cause;
volatile unsigned long last_epc;

/* An exception resumes AFTER the faulting instruction; an interrupt resumes ON
 * the interrupted one, which is why the two paths differ on `mepc`. The timer
 * has no acknowledge -- it is a comparison, not a latch -- so the handler must
 * move `mtimecmp` or it re-enters forever. */
__attribute__((aligned(4), naked)) void trap_entry(void)
{
    __asm__ volatile(
        "addi sp, sp, -32       \n"
        "sd   t0,  0(sp)        \n"
        "sd   t1,  8(sp)        \n"
        "sd   t2, 16(sp)        \n"

        "csrr t0, mcause        \n"
        "la   t1, last_cause    \n"
        "sd   t0, 0(t1)         \n"
        "csrr t2, mepc          \n"
        "la   t1, last_epc      \n"
        "sd   t2, 0(t1)         \n"
        "la   t1, trap_count    \n"
        "ld   t2, 0(t1)         \n"
        "addi t2, t2, 1         \n"
        "sd   t2, 0(t1)         \n"

        "bltz t0, 2f            \n"   /* mcause[63] set means interrupt */
        "csrr t1, mepc          \n"
        "addi t1, t1, 4         \n"
        "csrw mepc, t1          \n"
        "j    3f                \n"
        "2:                     \n"
        "li   t1, -1            \n"
        "csrw " MTIMECMP ", t1  \n"
        "3:                     \n"

        "ld   t0,  0(sp)        \n"
        "ld   t1,  8(sp)        \n"
        "ld   t2, 16(sp)        \n"
        "addi sp, sp, 32        \n"
        "mret                   \n"
    );
}

static int fails;

static void check(const char *what, unsigned long got, unsigned long want)
{
    if (got == want) return;
    ++fails;
    put_str("FAIL ");
    put_str(what);
    put_str(" got ");
    put_u64(got);
    put_str(" want ");
    put_u64(want);
    put_str("\n");
}

int main(void)
{
    /* --- plain read/write ------------------------------------------------ */
    csrw("mscratch", 0x0123456789abcdefUL);
    check("mscratch", csrr("mscratch"), 0x0123456789abcdefUL);

    /* --- the counters run ------------------------------------------------ */
    unsigned long c0 = csrr("mcycle");
    unsigned long c1 = csrr("mcycle");
    if (c1 <= c0) { ++fails; put_str("FAIL mcycle did not advance\n"); }

    unsigned long i0 = csrr("minstret");
    unsigned long i1 = csrr("minstret");
    if (i1 <= i0) { ++fails; put_str("FAIL minstret did not advance\n"); }

    /* mtime is the free-running one that must survive a halt. */
    unsigned long t0 = csrr("time");
    unsigned long t1 = csrr("time");
    if (t1 <= t0) { ++fails; put_str("FAIL mtime did not advance\n"); }

    /* --- traps ----------------------------------------------------------- */
    csrw("mtvec", (unsigned long)&trap_entry);

    unsigned long n = trap_count;
    __asm__ volatile ("ecall");
    check("ecall trap_count", trap_count, n + 1);
    check("ecall mcause", last_cause, CAUSE_ECALL_M);

    /* An address the CSR file does not implement must trap, not read zero. */
    n = trap_count;
    __asm__ volatile ("csrr t0, 0x7ff" ::: "t0");
    check("illegal trap_count", trap_count, n + 1);
    check("illegal mcause", last_cause, CAUSE_ILLEGAL);

    /* --- the timer interrupt --------------------------------------------- */
    n = trap_count;
    csrw(MTIMECMP, csrr("time") + 200);
    csrs("mie", 1UL << 7);        /* MTIE  */
    csrs("mstatus", 1UL << 3);    /* MIE   */

    unsigned long guard = 0;
    while (trap_count == n && ++guard < 100000) { }
    check("timer trap_count", trap_count, n + 1);
    check("timer mcause", last_cause, CAUSE_TIMER);

    /* The handler pushed mtimecmp to the top, so it must not fire again. */
    n = trap_count;
    for (guard = 0; guard < 5000; ++guard) { }
    check("timer stayed dismissed", trap_count, n);

    if (fails == 0) put_str("csr ok\n");
    return fails;
}
