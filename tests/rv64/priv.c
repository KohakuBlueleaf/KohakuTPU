/* Machine, supervisor and user mode: the switch in both directions, delegation,
 * and the rule that a privileged instruction below its own level is illegal.
 *
 * THE ESCAPE FROM USER MODE IS A DELIBERATE ILLEGAL INSTRUCTION. `sret` in U is
 * cause 2, which is not delegated, so it lands in the machine handler -- which
 * is how this test gets back to M without a second mechanism. */

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

#define CAUSE_ILLEGAL   2UL
#define CAUSE_ECALL_U   8UL
#define CAUSE_ECALL_S   9UL

volatile unsigned long m_count, s_count;
volatile unsigned long last_m_cause, last_s_cause;
volatile unsigned long u_ran, s_ran;
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

/* Machine handler. On the illegal instruction that user mode used to escape it
 * returns to `resume_addr` with MPP forced back to machine; everything else
 * simply steps over the faulting instruction. */
__attribute__((aligned(4), naked)) void m_trap(void)
{
    __asm__ volatile(
        "addi sp, sp, -32       \n"
        "sd   t0,  0(sp)        \n"
        "sd   t1,  8(sp)        \n"
        "sd   t2, 16(sp)        \n"

        "csrr t0, mcause        \n"
        "la   t1, last_m_cause  \n"
        "sd   t0, 0(t1)         \n"
        "la   t1, m_count       \n"
        "ld   t2, 0(t1)         \n"
        "addi t2, t2, 1         \n"
        "sd   t2, 0(t1)         \n"

        "li   t1, 2             \n"
        "bne  t0, t1, 1f        \n"
        /* the escape: come back in machine mode at resume_addr */
        "la   t1, resume_addr   \n"
        "ld   t2, 0(t1)         \n"
        "csrw mepc, t2          \n"
        "csrr t0, mstatus       \n"
        "li   t1, 0x1800        \n"
        "or   t0, t0, t1        \n"
        "csrw mstatus, t0       \n"
        "j    2f                \n"
        "1:                     \n"
        "csrr t1, mepc          \n"
        "addi t1, t1, 4         \n"
        "csrw mepc, t1          \n"
        "2:                     \n"

        "ld   t0,  0(sp)        \n"
        "ld   t1,  8(sp)        \n"
        "ld   t2, 16(sp)        \n"
        "addi sp, sp, 32        \n"
        "mret                   \n"
    );
}

/* Supervisor handler: steps over the ECALL and returns to whatever mode SPP
 * names, which for this test is always user. */
__attribute__((aligned(4), naked)) void s_trap(void)
{
    __asm__ volatile(
        "addi sp, sp, -32       \n"
        "sd   t0,  0(sp)        \n"
        "sd   t1,  8(sp)        \n"
        "sd   t2, 16(sp)        \n"

        "csrr t0, scause        \n"
        "la   t1, last_s_cause  \n"
        "sd   t0, 0(t1)         \n"
        "la   t1, s_count       \n"
        "ld   t2, 0(t1)         \n"
        "addi t2, t2, 1         \n"
        "sd   t2, 0(t1)         \n"

        "csrr t1, sepc          \n"
        "addi t1, t1, 4         \n"
        "csrw sepc, t1          \n"

        "ld   t0,  0(sp)        \n"
        "ld   t1,  8(sp)        \n"
        "ld   t2, 16(sp)        \n"
        "addi sp, sp, 32        \n"
        "sret                   \n"
    );
}

/* Runs in user mode. The ECALL is delegated to the supervisor handler; the
 * `sret` afterwards is illegal here and carries the test back to machine. */
__attribute__((aligned(4), naked)) void u_entry(void)
{
    __asm__ volatile(
        "la   t0, u_ran         \n"
        "li   t1, 1             \n"
        "sd   t1, 0(t0)         \n"
        "ecall                  \n"
        "la   t0, u_ran         \n"
        "li   t1, 2             \n"
        "sd   t1, 0(t0)         \n"
        "sret                   \n"      /* illegal in U: the escape */
        "1: j 1b                \n"
    );
}

/* Runs in supervisor mode, and drops straight to user. */
__attribute__((aligned(4), naked)) void s_entry(void)
{
    __asm__ volatile(
        "la   t0, s_ran         \n"
        "li   t1, 1             \n"
        "sd   t1, 0(t0)         \n"

        "la   t0, s_trap        \n"
        "csrw stvec, t0         \n"

        /* SPP = 0 so `sret` lands in user mode */
        "csrr t0, sstatus       \n"
        "li   t1, 0x100         \n"
        "not  t1, t1            \n"
        "and  t0, t0, t1        \n"
        "csrw sstatus, t0       \n"

        "la   t0, u_entry       \n"
        "csrw sepc, t0          \n"
        "sret                   \n"
    );
}

void phase2(void);

int main(void)
{
    csrw("mtvec", (unsigned long)&m_trap);

    /* ECALL from user, and both page faults, are the supervisor's business.
     * Illegal-instruction is deliberately NOT delegated -- it is the escape. */
    csrw("medeleg", (1UL << 8) | (1UL << 13) | (1UL << 15));
    csrw("mideleg", 0UL);

    resume_addr = (unsigned long)&phase2;

    /* MPP = supervisor, then `mret` is the mode switch. */
    unsigned long ms = csrr("mstatus");
    ms = (ms & ~(3UL << 11)) | (1UL << 11);
    csrw("mstatus", ms);
    csrw("mepc", (unsigned long)&s_entry);
    __asm__ volatile ("mret");

    put_str("FAIL mret did not leave machine mode\n");
    return 1;
}

/* Back in machine mode, by way of the illegal `sret` in user code. */
void phase2(void)
{
    check("supervisor entry ran", s_ran, 1);
    check("user entry ran", u_ran, 2);
    check("supervisor took one trap", s_count, 1);
    check("ECALL from U delegated", last_s_cause, CAUSE_ECALL_U);
    check("machine took one trap", m_count, 1);
    check("illegal sret in U", last_m_cause, CAUSE_ILLEGAL);
    /* MRET leaves MPP at U by definition, so it says nothing about the mode we
     * are in now. What proves machine mode is that a machine CSR answers at
     * all: the same access from supervisor or user is an illegal instruction. */
    check("MPP cleared by mret", (csrr("mstatus") >> 11) & 3UL, 0UL);
    csrw("mscratch", 0xfeedUL);
    check("mscratch after return", csrr("mscratch"), 0xfeedUL);
    check("no extra trap taken", m_count, 1);

    /* A misaligned load. The trap decision reads a REGISTERED copy of the
     * misalignment, so this also checks that the copy has settled by the time
     * the access retires and the boundary opens. */
    unsigned long n = m_count, junk;
    __asm__ volatile ("ld %0, 0(%1)" : "=r"(junk) : "r"(0x10001UL));
    (void)junk;
    check("misaligned load trapped", m_count, n + 1);
    check("misaligned load cause", last_m_cause, 4UL);

    n = m_count;
    __asm__ volatile ("sd x0, 0(%0)" :: "r"(0x10002UL));
    check("misaligned store trapped", m_count, n + 1);
    check("misaligned store cause", last_m_cause, 6UL);

    if (fails == 0) put_str("priv ok\n");

    *(volatile unsigned long *)0x10000008UL = (unsigned long)fails;
    for (;;) { }
}
