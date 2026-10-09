/* The interlink's signals, posted remote stores and the D-cache, as a node
 * program on rv64_node_pair uses them (sig_a.c, sig_b.c). */

#ifndef SIG_H
#define SIG_H

#define CTRL      0x00020000UL
#define REG(o)    (*(volatile unsigned long *)(CTRL + (o)))
#define CONSOLE   ((volatile unsigned char *)(CTRL + 0x08))

#define MV_STAT   0x20
#define DB_COUNTS 0x28
#define DCACHE    0x1C8
#define MV(r)     REG(0x80 + (r))
#define IL_CTL    0xC0          /* [0] enable [1] clear counts [3] clear slots */
#define IL_RING   0xD0          /* {fence[32], amount[31:16], slot[15:8], dst[1:0]} */
#define IL_CONS   0xE0          /* write: {dec[63:32], slot[7:0]}; read: sig word */
#define IL_SEL    0xE8          /* the slot the sig word reports */

#define STG(m)    (0x8000000000UL | ((unsigned long)(m) << 36))
#define UNC       (1UL << 38)
#define DRAM      0x80000000UL

static inline unsigned long cycles(void)
{
    unsigned long c;
    __asm__ volatile("csrr %0, mcycle" : "=r"(c));
    return c;
}

static void putch(char c) { *CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_dec(unsigned long v)
{
    char b[24];
    int n = 0;
    do { b[n++] = (char)('0' + v % 10); v /= 10; } while (v);
    while (n) putch(b[--n]);
}
static void put_hex(unsigned long v)
{
    for (int i = 60; i >= 0; i -= 4) {
        unsigned d = (unsigned)((v >> i) & 0xf);
        putch((char)(d < 10 ? '0' + d : 'a' + d - 10));
    }
}

static int fails;
static void check(const char *what, unsigned long got, unsigned long want)
{
    if (got == want) return;
    ++fails;
    put_str("FAIL "); put_str(what);
    put_str(" got "); put_hex(got);
    put_str(" want "); put_hex(want); putch('\n');
}
static void report(const char *what, unsigned long v)
{
    put_str("@ "); put_str(what); putch(' '); put_dec(v); putch('\n');
}

/* ---- signals ---- */
static unsigned long rings_written;

static inline unsigned long sig_word(void) { return REG(IL_CONS); }
static inline unsigned long rings_sent(void) { return (sig_word() >> 32) & 0xffffUL; }

/* At most four rings queued: bounded by the sent count, which is never ahead. */
static void ring(unsigned long dst, unsigned long slot, unsigned long amount, int fence)
{
    unsigned long g = 0;
    while (((rings_written - rings_sent()) & 0xffffUL) >= 4 && ++g < 1000000) { }
    REG(IL_RING) = ((unsigned long)(fence != 0) << 32) | (amount << 16) | (slot << 8) | dst;
    ++rings_written;
}

static void select_slot(unsigned long slot) { REG(IL_SEL) = slot; }

/* Wait until the selected slot holds at least `want`; returns the count. */
static unsigned long wait_slot(unsigned long slot, unsigned long want)
{
    select_slot(slot);
    unsigned long c, g = 0;
    /* the select lands two registers deep: the first reads may be another slot */
    for (int i = 0; i < 4; ++i) (void)sig_word();
    while (((c = sig_word() & 0xffffffffUL) < want) && ++g < 4000000) { }
    return c;
}

static void consume(unsigned long slot, unsigned long dec)
{
    REG(IL_CONS) = (dec << 32) | slot;
}

/* ---- the mover: one-dimensional copy of n 32-byte words ---- */
static void mover_copy(unsigned long src, unsigned long dst, unsigned long n)
{
    MV(0x10) = (1UL << 44) | (src << 4) | 0UL;
    MV(0x18) = (32UL << 20) | (n << 4) | (0UL << 1) | 0UL;
    MV(0x20) = 0UL;
    MV(0x10) = (1UL << 44) | (dst << 4) | 1UL;
    MV(0x18) = (32UL << 20) | (n << 4) | (0UL << 1) | 1UL;
    MV(0x20) = 0UL;
    MV(0x00) = (1UL << 16) | (1UL << 3) | 0UL;
}

static void mover_idle(void)
{
    unsigned long g = 0;
    while ((REG(MV_STAT) & (1UL << 32)) && ++g < 1000000) { }
}

static unsigned long pattern(unsigned long i, unsigned long seed)
{
    return (seed << 56) | (i * 0x0101010101UL) | 0x1000000000000UL;
}

#endif
