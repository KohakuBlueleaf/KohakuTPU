/* Milestone 3: the RV64 commands a real vector unit across the mesh.
 *
 * It writes two FP16 operand arrays into DRAM, programs vec0 at (x=1,y=0) over
 * the NoC mailbox -- staging the kernel word by word, then the descriptors --
 * runs it, and reads the sums back out of DRAM. The kernel is the one proven in
 * tests/vector/vec_cu_tb.v: VFILL DRAM->L1, VLD, VADD, VST, VDRAIN L1->DRAM,
 * with a[i]=i+1 and b[i]=2(i+1), so every sum is exactly 3(i+1).
 *
 * The mailbox carries the full 256-bit CU_INST payload: arg2 is its top 64 bits
 * (opcode at [255:252], target and descriptor value below it), arg0 the bottom,
 * where an imem word rides at [31:0]. See rv64_noc_mbox.v. */

#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))

#define NM(i)   (*(volatile unsigned long *)(CTRL_BASE + 0x40 + 8 * (i)))
#define NM_DST  0
#define NM_ARG0 1       /* payload[63:0]    */
#define NM_ARG1 2       /* payload[127:64]  */
#define NM_ARG2 3       /* payload[191:128] */
#define NM_ARG3 4       /* payload[255:192] -- opcode at [255:252] */
#define NM_GO   5
#define NM_STAT 6
#define NM_HEAD 7       /* read = completion head; write = pop */

#define SIG_FAULT 4

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_u64(unsigned long v) {
    char b[21]; int n = 0;
    if (!v) { putch('0'); return; }
    while (v) { b[n++] = (char)('0' + (v % 10)); v /= 10; }
    while (n) { putch(b[--n]); }
}

/* FP16 of a small non-negative integer, exact below 2048. Matches the f16i in
 * vec_cu_tb.v so the operands and the checks are bit-identical. */
static unsigned short f16i(unsigned v)
{
    unsigned t, ex, sh;
    if (v == 0) { return 0; }
    t = v; ex = 0;
    while (t > 1) { t >>= 1; ex++; }
    sh = (ex <= 10) ? (v << (10 - ex)) : (v >> (ex - 10));
    return (unsigned short)(((15 + ex) << 10) | (sh & 0x3ff));
}

#define VEC0 ((0UL << 8) | 1UL)     /* (x=1, y=0) */

static int fails;

/* Send one CU_INST and wait for its completion; a SIG_FAULT is a fail. Returns
 * the completion code, or 0xff if none arrived. */
static unsigned nm_send(unsigned long dst, unsigned long a3, unsigned long a2,
                        unsigned long a1, unsigned long a0)
{
    unsigned long head, code, guard;
    while (NM(NM_STAT) & (1UL << 15)) { }        /* tx still holds a flit */
    NM(NM_DST)  = dst;
    NM(NM_ARG3) = a3;
    NM(NM_ARG2) = a2;
    NM(NM_ARG1) = a1;
    NM(NM_ARG0) = a0;
    NM(NM_GO)   = 1UL;

    guard = 0;
    while (((NM(NM_STAT) & 0xff) == 0) && (++guard < 2000000)) { }
    if ((NM(NM_STAT) & 0xff) == 0) { return 0xff; }
    head = NM(NM_HEAD);
    code = (head >> 40) & 0xff;
    if (code == SIG_FAULT) { ++fails; put_str("FAIL fault code "); put_u64((head >> 8) & 0xffffffff); putch('\n'); }
    NM(NM_HEAD) = 1UL;                            /* pop */
    return (unsigned)code;
}

/* The opcode and its top fields live in payload[255:192] = ARG3; a vec imem word
 * rides at payload[31:0] = ARG0. ARG2/ARG1 (payload[191:64]) are unused here. */
static void put_imem(unsigned addr, unsigned word)
{
    nm_send(VEC0, (1UL << 60) | ((unsigned long)addr << 51), 0UL, 0UL, word);
}
static void put_desc(unsigned ad, unsigned fld, unsigned long v)
{
    nm_send(VEC0, (2UL << 60) | ((unsigned long)ad << 57)
            | ((unsigned long)fld << 54) | (v << 20), 0UL, 0UL, 0UL);
}
static void do_run(unsigned pc)
{
    nm_send(VEC0, (3UL << 60) | ((unsigned long)pc << 51), 0UL, 0UL, 0UL);
}

/* A bare op-0 dispatch: every unit retires it immediately, so a completion with
 * no fault proves the unit is reachable and answering on the mesh. */
static void ping(const char *name, unsigned long dst)
{
    unsigned code = nm_send(dst, 0UL, 0UL, 0UL, 0UL);
    if (code == 0xff) { ++fails; put_str("FAIL no reply from "); put_str(name); putch('\n'); }
}

int main(void)
{
    volatile unsigned short *a = (volatile unsigned short *)0x80001000UL;
    volatile unsigned short *b = (volatile unsigned short *)0x80001100UL;
    volatile unsigned short *s = (volatile unsigned short *)0x80002200UL;
    volatile unsigned long  *scratch = (volatile unsigned long *)0x80010000UL;
    unsigned i;

    /* Every unit on the mesh must answer a bare dispatch first. */
    ping("vec0", (0UL << 8) | 1UL);
    ping("mat0", (1UL << 8) | 1UL);
    ping("mat1", (1UL << 8) | 2UL);
    ping("vec1", (2UL << 8) | 1UL);
    if (!fails) { put_str("mesh: 4 units reachable\n"); }

    for (i = 0; i < 128; ++i) { a[i] = f16i(i + 1); b[i] = f16i(2 * (i + 1)); }
    /* Write-back L1: walk 4 KB of scratch to evict the operand lines so the
     * vector unit reads them from DRAM, not from lines still dirty here. */
    for (i = 0; i < 512; ++i) { scratch[i] = i; }

    put_imem(0,  0xD0000000);   /* VSETI     */
    put_imem(1,  128);
    put_imem(2,  0xC0000000);   /* VSETVL    */
    put_imem(3,  0xC8000000);   /* VSETMD    */
    put_imem(4,  0xE8000000);   /* VFILL     */
    put_imem(5,  0xE0000000);   /* VBAR      */
    put_imem(6,  0xA1200000);   /* VLD  -> v0 */
    put_imem(7,  0xA1420000);   /* VLD  -> v1 */
    put_imem(8,  0x18040220);   /* VADD v2   */
    put_imem(9,  0xA9640000);   /* VST  v2   */
    put_imem(10, 0xF0800000);   /* VDRAIN    */
    put_imem(11, 0xF8000000);   /* VHALT     */

    put_desc(0, 0, 0x1000);              /* A0 base = A_SRC        */
    put_desc(0, 1, (32UL << 16) | 16);   /* A0 stride 32, 16 lines */
    put_desc(1, 0, 0);                   /* A1 L1 word 0 (v0)      */
    put_desc(1, 1, (1UL << 16) | 8);
    put_desc(2, 0, 8);                   /* A2 L1 word 8 (v1)      */
    put_desc(2, 1, (1UL << 16) | 8);
    put_desc(3, 0, 16);                  /* A3 L1 word 16 (v2)     */
    put_desc(3, 1, (1UL << 16) | 8);
    put_desc(4, 0, 0x2000);              /* A4 base = A_DST        */
    put_desc(4, 1, (32UL << 16) | 24);   /* drain 24 lines         */

    do_run(0);

    /* The unit retires when its last VDRAIN beat is SENT, not landed, and this
     * L1 is not coherent with another unit's DRAM write -- so a read now would
     * cache a stale 0 forever. Wait for the writes to reach DRAM first; the
     * first read of each line is then a fresh miss that fetches the sum. */
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
        put_str("vadd ok  sum[0]="); put_u64(s[0]);
        put_str(" sum[127]="); put_u64(s[127]); putch('\n');
    }
    *R_EXIT = fails;
    for (;;) { }
}
