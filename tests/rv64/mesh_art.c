/* The SysNode dispatching COMPILER-GENERATED code.
 *
 * vadd_artifact.h is emitted straight from the KohakuTPU compiler
 * (ElementwiseKernel ADD, 128 fp16) -- the exact CU_INST payloads that, on a
 * card, the HOST would stage and kick. Here the on-chip RV64 replays them: it
 * puts the operands in DRAM, then walks the artifact's flits and sends each over
 * the NoC mailbox (one flit per GO), draining completions. The last flit is the
 * unit's RUN; the sums it drains to DRAM are read back and checked. No host. */

#include "vadd_artifact.h"

#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))

#define NM(i)   (*(volatile unsigned long *)(CTRL_BASE + 0x40 + 8 * (i)))
#define NM_DST  0
#define NM_ARG0 1
#define NM_ARG1 2
#define NM_ARG2 3
#define NM_ARG3 4
#define NM_GO   5
#define NM_STAT 6
#define NM_HEAD 7          /* read = head; write = pop */
#define SIG_FAULT 4

#define VEC0   ((0UL << 8) | 1UL)     /* the vector unit at (x=1, y=0), north */
#define VEC1   ((2UL << 8) | 1UL)     /* the vector unit at (x=1, y=2), south */
#ifndef TARGET_DST
#define TARGET_DST VEC0               /* which unit the artifact runs on */
#endif
#define DRAM   0x80000000UL           /* the RV64's window onto the mesh DRAM */

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_u64(unsigned long v) {
    char b[21]; int n = 0;
    if (!v) { putch('0'); return; }
    while (v) { b[n++] = (char)('0' + (v % 10)); v /= 10; }
    while (n) { putch(b[--n]); }
}

static unsigned short f16i(unsigned v)
{
    unsigned t, ex, sh;
    if (v == 0) { return 0; }
    t = v; ex = 0;
    while (t > 1) { t >>= 1; ex++; }
    sh = (ex <= 10) ? (v << (10 - ex)) : (v >> (ex - 10));
    return (unsigned short)(((15 + ex) << 10) | (sh & 0x3ff));
}

static int fails;

/* Send one CU_INST payload {a3,a2,a1,a0} to dst; wait for its completion. */
static void nm_send(unsigned long dst, const unsigned long *w)
{
    unsigned long guard = 0, head, code;
    while (NM(NM_STAT) & (1UL << 15)) { }
    NM(NM_DST)  = dst;
    NM(NM_ARG3) = w[0];
    NM(NM_ARG2) = w[1];
    NM(NM_ARG1) = w[2];
    NM(NM_ARG0) = w[3];
    NM(NM_GO)   = 1UL;
    while (((NM(NM_STAT) & 0xff) == 0) && (++guard < 4000000)) { }
    if ((NM(NM_STAT) & 0xff) == 0) { ++fails; put_str("FAIL no completion\n"); return; }
    head = NM(NM_HEAD);
    code = (head >> 40) & 0xff;
    if (code == SIG_FAULT) {
        ++fails; put_str("FAIL fault "); put_u64((head >> 8) & 0xffffffff); putch('\n');
    }
    NM(NM_HEAD) = 1UL;
}

int main(void)
{
    volatile unsigned short *a = (volatile unsigned short *)(DRAM | VADD_SRC0);
    volatile unsigned short *b = (volatile unsigned short *)(DRAM | VADD_SRC1);
    volatile unsigned short *s = (volatile unsigned short *)(DRAM | VADD_DST);
    volatile unsigned long  *scratch = (volatile unsigned long *)(DRAM | 0x10000UL);
    unsigned i;

    for (i = 0; i < 128; ++i) { a[i] = f16i(i + 1); b[i] = f16i(2 * (i + 1)); }
    for (i = 0; i < 512; ++i) { scratch[i] = i; }   /* evict operands to DRAM */

    /* Replay the compiler's artifact: every payload, in order, over the mailbox.
     * The RV64 does not interpret them -- it is the dispatcher the host used to be. */
    for (i = 0; i < VADD_NFLIT; ++i) { nm_send(TARGET_DST, VADD_FLITS[i]); }

    /* Let the unit's VDRAIN writes reach DRAM before the (incoherent) read. */
    for (volatile unsigned d = 0; d < 40000; ++d) { }

    for (i = 0; i < 128; ++i) {
        unsigned short want = f16i(3 * (i + 1));
        if (s[i] != want) {
            ++fails;
            put_str("FAIL sum["); put_u64(i); put_str("]=");
            put_u64(s[i]); put_str(" want "); put_u64(want); putch('\n');
            break;
        }
    }

    if (!fails) {
        put_str("art ok  "); put_u64(VADD_NFLIT);
        put_str(" flits  sum[0]="); put_u64(s[0]);
        put_str(" sum[127]="); put_u64(s[127]); putch('\n');
    }
    *R_EXIT = fails;
    for (;;) { }
}
