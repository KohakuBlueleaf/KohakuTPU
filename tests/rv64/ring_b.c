/* Mesh 1's processor, the other half of the pair. It waits for mesh 0's
 * doorbell AS AN INTERRUPT, reads the words mesh 0's mover put into its
 * staging, and rings mesh 0 back.
 *
 * The doorbell is a level: an inbound count stays pending until the handler
 * clears it through the window. The handler records the counts, clears them
 * and masks the line; the main loop checks the data and answers. */

#define CTRL      0x00020000UL
#define CONSOLE   ((volatile unsigned char *)(CTRL + 0x08))
#define DB_COUNTS (*(volatile unsigned long *)(CTRL + 0x28))
#define DB_CTL    (*(volatile unsigned long *)(CTRL + 0xC0))
#define DB_RING   (*(volatile unsigned long *)(CTRL + 0xD0))

#define STG_B     0x9000000000UL
#define OFF       0x100UL
#define WORDS     4

#define CAUSE_MEI 0x800000000000000BUL

static void putch(char c) { *CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_hex(unsigned long v) {
    for (int i = 60; i >= 0; i -= 4) {
        unsigned d = (unsigned)((v >> i) & 0xf);
        putch((char)(d < 10 ? '0' + d : 'a' + d - 10));
    }
}

#define csrw(name, v) \
    __asm__ volatile ("csrw " name ", %0" :: "r"((unsigned long)(v)))
#define csrs(name, v) \
    __asm__ volatile ("csrrs x0, " name ", %0" :: "r"((unsigned long)(v)))

volatile unsigned long irqs, irq_cause, irq_counts;
static int fails;

static void check(const char *what, unsigned long got, unsigned long want)
{
    if (got == want) return;
    ++fails;
    put_str("FAIL "); put_str(what);
    put_str(" got "); put_hex(got);
    put_str(" want "); put_hex(want); putch('\n');
}

static unsigned long pattern(unsigned long i)
{
    return 0xA000000000000000UL | (i * 0x0101010101010101UL);
}

/* Record the cause and the counts, clear the counts (the level), mask the
 * line, return. Ringing back is the main loop's job, after the data check. */
__attribute__((aligned(4), naked)) void m_trap(void)
{
    __asm__ volatile(
        "addi sp, sp, -32       \n"
        "sd   t0,  0(sp)        \n"
        "sd   t1,  8(sp)        \n"
        "sd   t2, 16(sp)        \n"
        "csrr t0, mcause        \n"
        "la   t1, irq_cause     \n"
        "sd   t0, 0(t1)         \n"
        "li   t1, 0x20028       \n"
        "ld   t0, 0(t1)         \n"
        "la   t1, irq_counts    \n"
        "sd   t0, 0(t1)         \n"
        "li   t1, 0x200c0       \n"
        "li   t0, 3             \n"
        "sd   t0, 0(t1)         \n"
        "la   t0, irqs          \n"
        "ld   t1, 0(t0)         \n"
        "addi t1, t1, 1         \n"
        "sd   t1, 0(t0)         \n"
        "li   t1, 0x800         \n"
        "csrc mie, t1           \n"
        "ld   t0,  0(sp)        \n"
        "ld   t1,  8(sp)        \n"
        "ld   t2, 16(sp)        \n"
        "addi sp, sp, 32        \n"
        "mret                   \n"
    );
}

int main(void)
{
    csrw("mtvec", (unsigned long)&m_trap);
    check("no ring yet", DB_COUNTS, 0UL);
    csrs("mie", 1UL << 11);       /* MEIE */
    csrs("mstatus", 1UL << 3);    /* MIE  */

    unsigned long guard = 0;
    while (irqs == 0 && ++guard < 4000000) { }
    check("one interrupt", irqs, 1UL);
    check("it was the external line", irq_cause, CAUSE_MEI);
    check("mesh 0 rang once", irq_counts & 0xffffUL, 1UL);
    check("and nobody else", irq_counts >> 16, 0UL);
    check("the handler cleared it", DB_COUNTS, 0UL);

    /* The words mesh 0's mover landed here, read through the node port. */
    volatile unsigned long *dst = (volatile unsigned long *)(STG_B + OFF);
    for (unsigned long i = 0; i < WORDS * 4; ++i)
        check("copied word", dst[i], pattern(i));

    /* Answer: destination mesh 0, tag 0x5B. */
    DB_RING = (0x5BUL << 8) | 0UL;

    if (fails == 0) put_str("ring b ok\n");
    return fails;
}
