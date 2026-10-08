/* Node control-region checks: a typed mailbox send (CU_CTRL caps read) and the
 * RX queue under backpressure, a mover FILL through the full mover window, and
 * D-cache flush/invalidate. The exit word is the number of failed checks. */

#define CTRL      0x00020000UL
#define REG(o)    (*(volatile unsigned long *)(CTRL + (o)))
#define CONSOLE   ((volatile unsigned char *)(CTRL + 0x08))

#define MB_DST    0x40
#define MB_ARG3   0x60
#define MB_GO     0x68
#define MB_STAT   0x70
#define RX_HDR    0x180
#define RX_P2     0x198
#define RX_P3     0x1A0
#define RX_USED   0x1A8
#define DCACHE    0x1C8
#define MV_STAT   0x20
#define MV(r)     REG(0x100 + (r))

#define T_CU_CTRL 0x7UL
#define MAT_X     1UL
#define MAT_Y     1UL
#define UNC       (1UL << 38)
#define DRAM      0x80000000UL

static void putch(char c) { *CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_hex(unsigned long v) {
    for (int i = 60; i >= 0; i -= 4) {
        unsigned d = (unsigned)((v >> i) & 0xf);
        putch((char)(d < 10 ? '0' + d : 'a' + d - 10));
    }
}

static int fails;
static void check(const char *what, unsigned long got, unsigned long want)
{
    if (got == want) { put_str("ok   "); put_str(what); putch('\n'); return; }
    ++fails;
    put_str("FAIL "); put_str(what);
    put_str(" got "); put_hex(got);
    put_str(" want "); put_hex(want); putch('\n');
}

/* one CU_CTRL read of index `idx` to the matmul unit; waits until the hub took it */
static void ctrl_read(unsigned long idx)
{
    REG(MB_DST) = (1UL << 24) | (T_CU_CTRL << 20) | (MAT_Y << 8) | MAT_X;
    REG(MB_ARG3) = idx << 48;
    REG(MB_GO) = 1;
    unsigned long g = 0;
    while ((REG(MB_STAT) & (1UL << 15)) && ++g < 100000) { }
}

static unsigned long rx_wait(void)
{
    unsigned long g = 0;
    while (!(REG(RX_HDR) >> 63) && ++g < 200000) { }
    return REG(RX_HDR);
}

int main(void)
{
    /* ---- mailbox: a caps read, reply whole in the RX queue ---- */
    ctrl_read(0);
    unsigned long h = rx_wait();
    check("RX has a reply", h >> 63, 1UL);
    check("reply type is CU_CTRL", (h >> 12) & 0xfUL, T_CU_CTRL);
    /* ctrl_val sits at flit[239:176]: P3[47:0] above P2[63:48] */
    unsigned long caps = ((REG(RX_P3) & 0xffffffffffffUL) << 16) | (REG(RX_P2) >> 48);
    check("matmul CU_TYPE", caps >> 48, 0x4D47UL);
    check("matmul CU_VERSION 6", (caps >> 40) & 0xffUL, 6UL);
    REG(RX_HDR) = 0;   /* pop */

    /* 12 replies, nothing popped: 8 queue, the rest held at the hub */
    for (int i = 0; i < 12; ++i) {
        ctrl_read(0);
        unsigned long g = 0;
        while (REG(RX_USED) < (unsigned long)(i < 8 ? i + 1 : 8) && ++g < 20000) { }
    }
    check("RX queue full at 8", REG(RX_USED), 8UL);
    int got = 0;
    for (unsigned long g = 0; got < 12 && g < 400000; ++g) {
        if (REG(RX_HDR) >> 63) { REG(RX_HDR) = 0; ++got; }
    }
    check("all 12 replies arrived", (unsigned long)got, 12UL);

    /* ---- mover: FILL with its immediate ---- */
    volatile unsigned long *f = (volatile unsigned long *)(UNC | (DRAM + 0x100000UL));
    for (int i = 0; i < 16; ++i) f[i] = 0;
    MV(0x40) = 0x5A5A1234UL;                                   /* imm */
    MV(0x10) = (1UL << 44) | ((DRAM + 0x100000UL) << 4) | 1UL;  /* dst header */
    MV(0x18) = (32UL << 20) | (4UL << 4) | (0UL << 1) | 1UL;    /* 4 x 32 B */
    MV(0x20) = 0UL;
    MV(0x00) = (1UL << 16) | (2UL << 3) | 4UL;                  /* go, 32-bit, FILL */
    unsigned long g = 0;
    while ((REG(MV_STAT) & (1UL << 32)) && ++g < 200000) { }
    check("mover fault", (REG(MV_STAT) >> 28) & 0xfUL, 0UL);
    check("FILL wrote the immediate", f[0] & 0xffffffffUL, 0x5A5A1234UL);
    check("FILL reached the last word", f[15] >> 32, 0x5A5A1234UL);

    /* ---- D-cache: flush and invalidate ---- */
    volatile unsigned long *c = (volatile unsigned long *)(DRAM + 0x200000UL);
    volatile unsigned long *u = (volatile unsigned long *)(UNC | (DRAM + 0x200000UL));
    *u = 0xAAAAUL;
    check("cached read fills", *c, 0xAAAAUL);
    *c = 0x1111UL;                       /* dirty in L1 */
    check("dirty line not in memory", *u, 0xAAAAUL);
    REG(DCACHE) = 1;                     /* flush */
    g = 0;
    while (REG(DCACHE) && ++g < 100000) { }
    check("flush wrote it back", *u, 0x1111UL);
    *u = 0x2222UL;                       /* memory changes behind the cache */
    check("cached read still stale", *c, 0x1111UL);
    REG(DCACHE) = 2;                     /* invalidate */
    g = 0;
    while (REG(DCACHE) && ++g < 100000) { }
    check("invalidate refetches", *c, 0x2222UL);

    if (fails == 0) put_str("hw bundle ok\n");
    return fails;
}
