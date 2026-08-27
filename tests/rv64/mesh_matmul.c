/* The SysNode dispatching a COMPILER-GENERATED matmul to the real mat unit.
 *
 * matmul_artifact.h is emitted from the KohakuTPU cluster ISA (a 4x32 @ 4x32
 * GEMM): four CU_INST payloads (FILL A, FILL B, GEMM, DRAIN) plus the fp16
 * operands and the offline model's golden result. The RV64 writes A and B to
 * DRAM, replays the four flits to mat0 over the NoC mailbox, and reads the 4x4
 * result back. The cluster payloads use payload[191:128] -- reachable only
 * because the mailbox now carries the whole 256 bits. */

#include "matmul_artifact.h"

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
#define NM_HEAD 7
#define SIG_FAULT 4

#define MAT0   ((1UL << 8) | 1UL)     /* the matmul cluster at (x=1, y=1), local */
#define DRAM   0x80000000UL

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_hex(unsigned v) {
    int i;
    for (i = 12; i >= 0; i -= 4) { putch("0123456789abcdef"[(v >> i) & 0xf]); }
}

static int fails;

/* Fire a flit without waiting for its completion. The cluster retires GEMM on
 * issue and its DRAIN only waits on !gemm_busy, so FILL/GEMM/DRAIN must stream
 * back-to-back through its inst FIFO -- exactly as the orchestrator sends them.
 * Throttle only on tx-free; completions are drained afterwards. */
static void nm_fire(unsigned long dst, const unsigned long *w)
{
    while (NM(NM_STAT) & (1UL << 15)) { }
    NM(NM_DST)  = dst;
    NM(NM_ARG3) = w[0];
    NM(NM_ARG2) = w[1];
    NM(NM_ARG1) = w[2];
    NM(NM_ARG0) = w[3];
    NM(NM_GO)   = 1UL;
}

static void nm_drain(unsigned n)
{
    unsigned got = 0;
    unsigned long guard = 0, head;
    while (got < n && ++guard < 8000000) {
        if (NM(NM_STAT) & 0xff) {
            head = NM(NM_HEAD);
            if (((head >> 40) & 0xff) == SIG_FAULT) { ++fails; put_str("FAIL fault\n"); }
            NM(NM_HEAD) = 1UL;
            ++got;
        }
    }
    if (got < n) { ++fails; put_str("FAIL missing completions\n"); }
}

int main(void)
{
    volatile unsigned short *A = (volatile unsigned short *)(DRAM | MM_A_ADDR);
    volatile unsigned short *B = (volatile unsigned short *)(DRAM | MM_B_ADDR);
    volatile unsigned short *C = (volatile unsigned short *)(DRAM | MM_C_ADDR);
    volatile unsigned long  *scratch = (volatile unsigned long *)(DRAM | 0x10000UL);
    unsigned i;

    /* MXFP7 entries: 64 uint16 = 128 bytes each (4 words), not fp16. */
    for (i = 0; i < 64; ++i) { A[i] = MM_A[i]; B[i] = MM_B[i]; }
    for (i = 0; i < 512; ++i) { scratch[i] = i; }   /* evict operands to DRAM */

    for (i = 0; i < MM_NFLIT; ++i) { nm_fire(MAT0, MM_FLITS[i]); }
    nm_drain(MM_NFLIT);

    for (volatile unsigned d = 0; d < 40000; ++d) { }   /* let DRAIN land */

    put_str("C = ");
    for (i = 0; i < 16; ++i) {
        put_hex(C[i]); putch(' ');
        if (C[i] != MM_C_GOLD[i]) { ++fails; }
    }
    putch('\n');
    if (!fails) { put_str("matmul ok\n"); }
    *R_EXIT = fails;
    for (;;) { }
}
