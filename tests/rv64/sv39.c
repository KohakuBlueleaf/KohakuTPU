/* Sv39 on the assembled SysCore: real page tables in node memory, walked by the
 * hardware, with translation proved by writing through a virtual address and
 * reading the physical one back from machine mode.
 *
 * THE KERNEL MUST MAP ITSELF FIRST. Translation covers every data access, so the
 * program's own globals, its stack and the console all need identity entries
 * before `mret` turns it on -- miss one and the first push page-faults.
 *
 * INSTRUCTION FETCH TRANSLATES TOO, through one page register in the wrapper:
 * the kernel maps its own text before `mret`, and a jump into an unmapped page
 * arrives in the handler as an instruction page fault (cause 12) whose `sepc`
 * is the bad address itself -- so the handler resumes somewhere it names. */

#define CONSOLE ((volatile unsigned char *)0x00020008UL)

static void putch(char c) { *CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_hex(unsigned long v) {
    for (int i = 60; i >= 0; i -= 4) {
        unsigned d = (unsigned)((v >> i) & 0xf);
        putch((char)(d < 10 ? '0' + d : 'a' + d - 10));
    }
}

#define csrr(name) ({ unsigned long v; \
    __asm__ volatile ("csrr %0, " name : "=r"(v)); v; })
#define csrw(name, v) \
    __asm__ volatile ("csrw " name ", %0" :: "r"((unsigned long)(v)))

#define PTE_V 1UL
#define PTE_R 2UL
#define PTE_W 4UL
#define PTE_X 0x08UL
#define PTE_U 0x10UL
#define PTE_A 0x40UL
#define PTE_D 0x80UL
#define LEAF  (PTE_V | PTE_R | PTE_W | PTE_A | PTE_D)
/* NO `U` HERE. Supervisor may not fetch from a user page at all -- `SUM`
 * relaxes loads and stores and never instruction fetch -- so kernel text and
 * user text cannot share one page. Everything in this test runs in S. */
#define TEXT  (PTE_V | PTE_R | PTE_X | PTE_A)

/* Uncached node range: the walker reads through the node port, and keeping the
 * tables out of the cached half removes the question of whether a table write
 * and a table walk see the same bytes. */
#define PT_ROOT 0x20000000UL
#define PT_L1   0x20001000UL
#define PT_L0   0x20002000UL
#define DATA_PA 0x20003000UL

#define DATA_VA 0x00040000UL
#define BAD_VA  0x00050000UL
#define MAGIC   0x5439c0ffeeUL

#define CAUSE_INSTR_PAGE_FAULT 12UL
#define CAUSE_LOAD_PAGE_FAULT  13UL
#define CAUSE_ECALL_S           9UL

volatile unsigned long s_cause, s_tval, s_faults, s_reached;
volatile unsigned long s_ifaults, s_itval, s_iepc, fetch_resume;
volatile unsigned long resume_addr;
static int fails;

static void check(const char *what, unsigned long got, unsigned long want)
{
    if (got == want) return;
    ++fails;
    put_str("FAIL "); put_str(what);
    put_str(" got "); put_hex(got);
    put_str(" want "); put_hex(want); putch('\n');
}

static void wr64(unsigned long pa, unsigned long v)
{
    *(volatile unsigned long *)pa = v;
}
static unsigned long rd64(unsigned long pa)
{
    return *(volatile unsigned long *)pa;
}
static unsigned long pte(unsigned long ppn, unsigned long perm)
{
    return (ppn << 10) | perm;
}

/* Machine handler. An ECALL from supervisor is how the test comes home. */
__attribute__((aligned(4), naked)) void m_trap(void)
{
    __asm__ volatile(
        "addi sp, sp, -16       \n"
        "sd   t0,  0(sp)        \n"
        "sd   t1,  8(sp)        \n"
        "la   t0, resume_addr   \n"
        "ld   t1, 0(t0)         \n"
        "csrw mepc, t1          \n"
        "csrr t0, mstatus       \n"
        "li   t1, 0x1800        \n"
        "or   t0, t0, t1        \n"
        "csrw mstatus, t0       \n"
        "ld   t0,  0(sp)        \n"
        "ld   t1,  8(sp)        \n"
        "addi sp, sp, 16        \n"
        "mret                   \n"
    );
}

/* Supervisor handler: record the fault and step over the access that took it.
 * An INSTRUCTION fault cannot be stepped over -- `sepc` IS the bad page -- so
 * it resumes at whatever `fetch_resume` names. */
__attribute__((aligned(4), naked)) void s_trap(void)
{
    __asm__ volatile(
        "addi sp, sp, -32       \n"
        "sd   t0,  0(sp)        \n"
        "sd   t1,  8(sp)        \n"
        "sd   t2, 16(sp)        \n"
        "csrr t0, scause        \n"
        "la   t1, s_cause       \n"
        "sd   t0, 0(t1)         \n"
        "csrr t2, stval         \n"
        "la   t1, s_tval        \n"
        "sd   t2, 0(t1)         \n"
        "la   t1, s_faults      \n"
        "ld   t2, 0(t1)         \n"
        "addi t2, t2, 1         \n"
        "sd   t2, 0(t1)         \n"
        "li   t2, 12            \n"
        "bne  t0, t2, 1f        \n"
        "la   t1, s_ifaults     \n"
        "ld   t2, 0(t1)         \n"
        "addi t2, t2, 1         \n"
        "sd   t2, 0(t1)         \n"
        "csrr t2, stval         \n"
        "la   t1, s_itval       \n"
        "sd   t2, 0(t1)         \n"
        "csrr t2, sepc          \n"
        "la   t1, s_iepc        \n"
        "sd   t2, 0(t1)         \n"
        "la   t1, fetch_resume  \n"
        "ld   t1, 0(t1)         \n"
        "csrw sepc, t1          \n"
        "j    2f                \n"
        "1:                     \n"
        "csrr t1, sepc          \n"
        "addi t1, t1, 4         \n"
        "csrw sepc, t1          \n"
        "2:                     \n"
        "ld   t0,  0(sp)        \n"
        "ld   t1,  8(sp)        \n"
        "ld   t2, 16(sp)        \n"
        "addi sp, sp, 32        \n"
        "sret                   \n"
    );
}

/* Runs in supervisor mode with translation on. */
void s_main(void)
{
    s_reached = 1;

    /* Through the virtual address. Machine mode checks the physical one. */
    *(volatile unsigned long *)DATA_VA = MAGIC;

    /* And read it back through the same mapping. */
    unsigned long back = *(volatile unsigned long *)DATA_VA;
    if (back != MAGIC) { ++fails; put_str("FAIL readback in S\n"); }

    /* A jump into the unmapped page: the fetch faults, `sepc` is BAD_VA, and
     * the handler lands here at label 1. Nothing at BAD_VA ever executes. */
    __asm__ volatile (
        "la   t0, 1f            \n"
        "la   t1, fetch_resume  \n"
        "sd   t0, 0(t1)         \n"
        "mv   t1, %0            \n"
        "jalr ra, 0(t1)         \n"
        "1:                     \n"
        :: "r"(BAD_VA) : "t0", "t1", "ra", "memory");

    /* An unmapped page: one load, one fault, and the handler steps over it. */
    unsigned long junk;
    __asm__ volatile ("ld %0, 0(%1)" : "=r"(junk) : "r"(BAD_VA));
    (void)junk;

    __asm__ volatile ("ecall");     /* cause 9, not delegated: back to machine */
    for (;;) { }
}

void phase2(void);

int main(void)
{
    /* ---- tables, built from machine mode with translation still off ------ */
    wr64(PT_ROOT, pte(PT_L1 >> 12, PTE_V));
    wr64(PT_L1,   pte(PT_L0 >> 12, PTE_V));

    /* Instruction fetch translates too, so the text pages come first: a kernel
     * that does not map itself faults on the instruction after `mret`. */
    wr64(PT_L0 + 0 * 8, pte(0, TEXT));
    wr64(PT_L0 + 1 * 8, pte(1, TEXT));

    /* The kernel's own world: SPAD 0x10000..0x17fff and the control page. */
    for (unsigned long p = 0x10; p <= 0x20; ++p)
        wr64(PT_L0 + p * 8, pte(p, LEAF));

    /* One page that is not where its virtual address says. */
    wr64(PT_L0 + (DATA_VA >> 12) * 8, pte(DATA_PA >> 12, LEAF));
    /* BAD_VA is deliberately left with no entry at all. */

    /* BOTH handlers go in before translation does. A fault taken with `stvec`
     * still zero has nowhere to go, and the core halts instead of trapping. */
    csrw("mtvec", (unsigned long)&m_trap);
    csrw("stvec", (unsigned long)&s_trap);
    csrw("medeleg", (1UL << 13) | (1UL << 15) | (1UL << 12));
    csrw("mideleg", 0UL);
    csrw("satp", (8UL << 60) | (PT_ROOT >> 12));

    resume_addr = (unsigned long)&phase2;

    unsigned long ms = csrr("mstatus");
    ms = (ms & ~(3UL << 11)) | (1UL << 11);     /* MPP = supervisor */
    csrw("mstatus", ms);
    csrw("mepc", (unsigned long)&s_main);
    __asm__ volatile ("mret");

    put_str("FAIL never left machine mode\n");
    return 1;
}

/* Machine mode again, translation off: physical addresses are literal here. */
void phase2(void)
{
    csrw("satp", 0UL);

    check("supervisor ran", s_reached, 1);
    check("two page faults", s_faults, 2);
    check("fault cause", s_cause, CAUSE_LOAD_PAGE_FAULT);
    check("fault address", s_tval, BAD_VA);
    check("one instruction fault", s_ifaults, 1);
    check("ifault address", s_itval, BAD_VA);
    check("ifault sepc", s_iepc, BAD_VA);

    /* THE PROOF: the store went to a virtual address and the bytes are at the
     * physical one the tables named, which no untranslated access could do. */
    check("translated store landed", rd64(DATA_PA), MAGIC);

    if (fails == 0) put_str("sv39 ok\n");

    *(volatile unsigned long *)0x00020020UL = (unsigned long)fails;
    for (;;) { }
}
