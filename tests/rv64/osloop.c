/* The shape an OS actually needs: user code running under Sv39 on its own
 * pages, preempted by the timer, resumed where it left off.
 *
 * THE TIMER IS NOT DELEGATED, AND THAT IS A DESIGN FACT. `mtimecmp` is a machine
 * CSR, so a supervisor handler could not dismiss the interrupt it was given --
 * there is no `stimecmp` here. Preemption is therefore machine-mode work, and
 * the supervisor handles only what it can finish: ECALL and page faults. */

#define CONSOLE ((volatile unsigned char *)0x00020008UL)

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

#define PTE_V 1UL
#define PTE_R 2UL
#define PTE_W 4UL
#define PTE_U 0x10UL
#define PTE_A 0x40UL
#define PTE_D 0x80UL
#define PTE_X 0x08UL
#define KERN  (PTE_V | PTE_R | PTE_W | PTE_A | PTE_D)
#define USER  (KERN | PTE_U)
/* TWO TEXT MAPPINGS, NOT ONE. Supervisor may not fetch from a user page --
 * `SUM` relaxes loads and stores and never instruction fetch -- so the user
 * routine is pushed onto its own page and only that page carries `U`. */
#define KTEXT (PTE_V | PTE_R | PTE_X | PTE_A)
#define UTEXT (KTEXT | PTE_U)

#define PT_ROOT 0x20000000UL
#define PT_L1   0x20001000UL
#define PT_L0   0x20002000UL
#define USER_PA 0x20004000UL
#define USER_VA 0x00100000UL

#define SLICES 6UL

volatile unsigned long preempts, last_mpp, s_calls;
volatile unsigned long resume_addr;
static int fails;

static void check(const char *what, unsigned long got, unsigned long want)
{
    if (got == want) return;
    ++fails;
    put_str("FAIL "); put_str(what);
    put_str(" got "); put_u64(got);
    put_str(" want "); put_u64(want); putch('\n');
}

static void wr64(unsigned long pa, unsigned long v)
{ *(volatile unsigned long *)pa = v; }
static unsigned long rd64(unsigned long pa)
{ return *(volatile unsigned long *)pa; }
static unsigned long pte(unsigned long ppn, unsigned long perm)
{ return (ppn << 10) | perm; }

/* Machine handler. Every timer tick is a preemption of whatever was running;
 * after SLICES of them the test is over and control goes to `resume_addr`. */
__attribute__((aligned(4), naked)) void m_trap(void)
{
    __asm__ volatile(
        "addi sp, sp, -32       \n"
        "sd   t0,  0(sp)        \n"
        "sd   t1,  8(sp)        \n"
        "sd   t2, 16(sp)        \n"

        /* which mode did we interrupt? */
        "csrr t0, mstatus       \n"
        "srli t1, t0, 11        \n"
        "andi t1, t1, 3         \n"
        "la   t2, last_mpp      \n"
        "sd   t1, 0(t2)         \n"

        "la   t1, preempts      \n"
        "ld   t2, 0(t1)         \n"
        "addi t2, t2, 1         \n"
        "sd   t2, 0(t1)         \n"

        /* push the compare forward: the timer is a comparison, not a latch */
        "csrr t0, time          \n"
        "li   t1, 400           \n"
        "add  t0, t0, t1        \n"
        "csrw " MTIMECMP ", t0  \n"

        "la   t1, preempts      \n"
        "ld   t2, 0(t1)         \n"
        "li   t1, %0            \n"
        "blt  t2, t1, 1f        \n"
        /* done: come back in machine mode at resume_addr */
        "la   t1, resume_addr   \n"
        "ld   t2, 0(t1)         \n"
        "csrw mepc, t2          \n"
        "csrr t0, mstatus       \n"
        "li   t1, 0x1800        \n"
        "or   t0, t0, t1        \n"
        "csrw mstatus, t0       \n"
        "1:                     \n"

        "ld   t0,  0(sp)        \n"
        "ld   t1,  8(sp)        \n"
        "ld   t2, 16(sp)        \n"
        "addi sp, sp, 32        \n"
        "mret                   \n"
        :: "i"(SLICES)
    );
}

/* Supervisor handler: ECALL from user is the only thing delegated here. */
__attribute__((aligned(4), naked)) void s_trap(void)
{
    __asm__ volatile(
        "addi sp, sp, -16       \n"
        "sd   t0,  0(sp)        \n"
        "sd   t1,  8(sp)        \n"
        "la   t0, s_calls       \n"
        "ld   t1, 0(t0)         \n"
        "addi t1, t1, 1         \n"
        "sd   t1, 0(t0)         \n"
        "csrr t0, sepc          \n"
        "addi t0, t0, 4         \n"
        "csrw sepc, t0          \n"
        "ld   t0,  0(sp)        \n"
        "ld   t1,  8(sp)        \n"
        "addi sp, sp, 16        \n"
        "sret                   \n"
    );
}

/* User mode, under translation, with no stack: it counts in its own page and
 * calls into the supervisor once so the delegated path is exercised too. */
__attribute__((section(".utext"), aligned(4), naked)) void u_loop(void)
{
    __asm__ volatile(
        "li   t0, %0            \n"
        "ld   t1, 0(t0)         \n"
        "addi t1, t1, 1         \n"
        "sd   t1, 0(t0)         \n"
        "ecall                  \n"
        "1:                     \n"
        "li   t0, %0            \n"
        "ld   t1, 0(t0)         \n"
        "addi t1, t1, 1         \n"
        "sd   t1, 0(t0)         \n"
        "j    1b                \n"
        :: "i"(USER_VA)
    );
}

__attribute__((aligned(4), naked)) void s_entry(void)
{
    __asm__ volatile(
        /* SPP = 0 so `sret` lands in user mode */
        "csrr t0, sstatus       \n"
        "li   t1, 0x100         \n"
        "not  t1, t1            \n"
        "and  t0, t0, t1        \n"
        "csrw sstatus, t0       \n"
        "la   t0, u_loop        \n"
        "csrw sepc, t0          \n"
        "sret                   \n"
    );
}

void phase2(void);

int main(void)
{
    wr64(PT_ROOT, pte(PT_L1 >> 12, PTE_V));
    wr64(PT_L1,   pte(PT_L0 >> 12, PTE_V));

    /* Fetch translates, so the text pages come first. `u_loop` is aligned onto
     * its own page precisely so it can be the only one the user may execute. */
    unsigned long up = ((unsigned long)&u_loop) >> 12;
    for (unsigned long p = 0; p < 4; ++p)
        wr64(PT_L0 + p * 8, pte(p, (p == up) ? UTEXT : KTEXT));

    /* The kernel's own world, with no U bit: user code cannot reach it. */
    for (unsigned long p = 0x10; p <= 0x20; ++p)
        wr64(PT_L0 + p * 8, pte(p, KERN));

    /* One page the user owns. */
    wr64(PT_L0 + (USER_VA >> 12) * 8, pte(USER_PA >> 12, USER));
    wr64(USER_PA, 0UL);

    csrw("mtvec", (unsigned long)&m_trap);
    csrw("stvec", (unsigned long)&s_trap);
    csrw("medeleg", (1UL << 8) | (1UL << 13) | (1UL << 15) | (1UL << 12));
    csrw("mideleg", 0UL);           /* the timer stays machine's, see the header */
    csrw("satp", (8UL << 60) | (PT_ROOT >> 12));

    resume_addr = (unsigned long)&phase2;

    csrw(MTIMECMP, csrr("time") + 400);
    csrs("mie", 1UL << 7);          /* MTIE */

    unsigned long ms = csrr("mstatus");
    ms = (ms & ~(3UL << 11)) | (1UL << 11);     /* MPP = supervisor */
    csrw("mstatus", ms);
    csrw("mepc", (unsigned long)&s_entry);
    __asm__ volatile ("mret");

    put_str("FAIL never left machine mode\n");
    return 1;
}

void phase2(void)
{
    csrw("mie", 0UL);
    csrw("satp", 0UL);

    check("preemptions", preempts, SLICES);
    check("interrupted user mode", last_mpp, 0UL);
    check("one delegated ECALL", s_calls, 1UL);

    /* The user page advanced, and it advanced at its PHYSICAL address -- which
     * only happens if the user's virtual store was translated. */
    unsigned long ticks = rd64(USER_PA);
    if (ticks < 2) {
        ++fails;
        put_str("FAIL user counter did not advance, got ");
        put_u64(ticks); putch('\n');
    }

    if (fails == 0) {
        put_str("osloop ok, user ticks ");
        put_u64(ticks);
        putch('\n');
    }

    *(volatile unsigned long *)0x00020020UL = (unsigned long)fails;
    for (;;) { }
}
