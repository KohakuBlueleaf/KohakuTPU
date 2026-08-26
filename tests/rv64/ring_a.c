/* Mesh 0's processor, one of a pair on the interlink. It writes a pattern into
 * its own staging, has the mover copy it into mesh 1's staging across the
 * link, rings mesh 1's doorbell, and waits for mesh 1 to ring back.
 *
 * Everything here is what a runtime on one node does to hand work to the next
 * one: data first, then the doorbell, then wait on the far side's doorbell.
 * Nothing crosses the link by a load or a store -- the processor's own port is
 * local, the mover's is not. */

#define CTRL      0x00020000UL
#define CONSOLE   ((volatile unsigned char *)(CTRL + 0x08))

/* control-region words */
#define MV_STAT   (*(volatile unsigned long *)(CTRL + 0x20))
#define DB_COUNTS (*(volatile unsigned long *)(CTRL + 0x28))
/* the mover's configuration window, 0x80 + register */
#define MV(r)     (*(volatile unsigned long *)(CTRL + 0x80 + (r)))
/* the interlink's window: +0 enable/clear, +8 mesh id, +0x10 ring */
#define DB_CTL    (*(volatile unsigned long *)(CTRL + 0xC0))
#define DB_RING   (*(volatile unsigned long *)(CTRL + 0xD0))

/* Staging aperture 0 of each mesh: [39] special, [37:36] mesh, [35:32] 0. */
#define STG_A     0x8000000000UL
#define STG_B     0x9000000000UL
#define OFF       0x100UL
#define WORDS     4                 /* 32-byte words the mover moves */

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

/* The mover's copy: a source header and one dimension, a destination header
 * and one dimension, then go. Register layouts are mm_mover's:
 *   0x10 {ndim[46:44], base[43:4], sel[0]}      sel 1 = destination
 *   0x18 {stride[51:20], count[19:4], dim[3:1], sel[0]}
 *   0x20 {astep[17:2], axis[1:0]}
 *   0x00 {go[16], flags[15:8], ewidth[4:3], mode[2:0]}   mode 0 = COPY */
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

int main(void)
{
    /* 1. the payload, into this mesh's own staging through the node port */
    volatile unsigned long *src = (volatile unsigned long *)(STG_A + OFF);
    for (unsigned long i = 0; i < WORDS * 4; ++i) src[i] = pattern(i);
    for (unsigned long i = 0; i < WORDS * 4; ++i)
        check("staging readback", src[i], pattern(i));

    /* 2. across the link with the mover; busy is bit 32, fault [31:28] */
    mover_copy(STG_A + OFF, STG_B + OFF, WORDS);
    unsigned long guard = 0;
    while ((MV_STAT & (1UL << 32)) && ++guard < 200000) { }
    check("mover finished", (MV_STAT >> 32) & 1UL, 0UL);
    check("mover fault", (MV_STAT >> 28) & 0xfUL, 0UL);
    /* done [27:0] counts descriptors: idle-and-zero means it never started */
    check("mover ran", MV_STAT & 0xfffffffUL, 1UL);

    /* 3. ring mesh 1: destination in [1:0], a transaction tag in [15:8] */
    check("no ring yet from mesh 1", (DB_COUNTS >> 16) & 0xffffUL, 0UL);
    DB_RING = (0xA5UL << 8) | 1UL;

    /* 4. wait for mesh 1's answer: its count is lane 1, bits [31:16] */
    guard = 0;
    while ((((DB_COUNTS >> 16) & 0xffffUL) == 0) && ++guard < 2000000) { }
    check("mesh 1 rang back", (DB_COUNTS >> 16) & 0xffffUL, 1UL);
    check("and nothing else did", DB_COUNTS & 0xffffUL, 0UL);

    /* 5. clear, and see the level drop. The clear is a config write that
     * lands two registers deep, so the first read back may still be early. */
    DB_CTL = 0x3UL;
    guard = 0;
    while (DB_COUNTS != 0 && ++guard < 64) { }
    check("counts cleared", DB_COUNTS, 0UL);

    if (fails == 0) put_str("ring a ok\n");
    return fails;
}
