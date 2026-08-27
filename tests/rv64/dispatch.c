/* The node commanding the mesh: dispatch a compute unit, take the completion
 * as an interrupt, read it out of the queue.
 *
 * THIS IS WHAT THE SHELL USED TO DO AND NO LONGER CAN. SysCore has no
 * compute-unit shell, so there is no kick-and-report lifecycle wrapped around
 * it; dispatch is a mailbox in the control region and a completion raises the
 * external interrupt line. */

#define CTRL      0x00020000UL
#define CONSOLE   ((volatile unsigned char *)(CTRL + 0x08))

/* the NoC mailbox window, 8-byte spaced */
#define NM(i)     (*(volatile unsigned long *)(CTRL + 0x40 + 8 * (i)))
#define NM_DST    0
#define NM_ARG0   1
#define NM_ARG1   2
#define NM_GO     5
#define NM_STAT   6
#define NM_HEAD   7
#define NM_POP    7       /* pop = a write to HEAD */

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

volatile unsigned long irqs;
static int fails;

static void check(const char *what, unsigned long got, unsigned long want)
{
    if (got == want) return;
    ++fails;
    put_str("FAIL "); put_str(what);
    put_str(" got "); put_u64(got);
    put_str(" want "); put_u64(want); putch('\n');
}

/* The completion raises the external line. The handler only counts it and
 * masks the source: draining the queue is the scheduler's job, not the
 * handler's, and leaving the line asserted would re-enter forever. */
__attribute__((aligned(4), naked)) void m_trap(void)
{
    __asm__ volatile(
        "addi sp, sp, -16       \n"
        "sd   t0,  0(sp)        \n"
        "sd   t1,  8(sp)        \n"
        "la   t0, irqs          \n"
        "ld   t1, 0(t0)         \n"
        "addi t1, t1, 1         \n"
        "sd   t1, 0(t0)         \n"
        "li   t1, 0x800         \n"
        "csrc mie, t1           \n"   /* mask MEIE; the queue is still there */
        "ld   t0,  0(sp)        \n"
        "ld   t1,  8(sp)        \n"
        "addi sp, sp, 16        \n"
        "mret                   \n"
    );
}

int main(void)
{
    csrw("mtvec", (unsigned long)&m_trap);

    check("queue starts empty", NM(NM_STAT) & 0xff, 0UL);

    /* Dispatch to the unit at (y=1, x=2) with a payload the peer echoes. */
    NM(NM_DST)  = (1UL << 8) | 2UL;
    NM(NM_ARG0) = 0xABCDEUL;
    NM(NM_ARG1) = 0UL;

    csrs("mie", 1UL << 11);       /* MEIE */
    csrs("mstatus", 1UL << 3);    /* MIE  */

    NM(NM_GO) = 1UL;

    unsigned long guard = 0;
    while (irqs == 0 && ++guard < 200000) { }
    check("completion raised an interrupt", irqs, 1UL);

    unsigned long stat = NM(NM_STAT);
    check("one completion queued", stat & 0xff, 1UL);

    unsigned long head = NM(NM_HEAD);
    /* [55:52] src_y, [51:48] src_x, [47:40] code, [39:8] arg. The peer answers
     * from where the dispatch was sent, with the argument it was given. */
    unsigned long arg  = (head >> 8)  & 0xffffffffUL;
    unsigned long code = (head >> 40) & 0xffUL;
    unsigned long sx   = (head >> 48) & 0xfUL;
    unsigned long sy   = (head >> 52) & 0xfUL;

    check("completion code", code, 0UL);
    check("completion arg echoed", arg, 0xABCDEUL);
    check("completion source x", sx, 2UL);
    check("completion source y", sy, 1UL);

    NM(NM_POP) = 1UL;
    check("queue drained", NM(NM_STAT) & 0xff, 0UL);

    if (fails == 0) put_str("dispatch ok\n");
    return fails;
}
