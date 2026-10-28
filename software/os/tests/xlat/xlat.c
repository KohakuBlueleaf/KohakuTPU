/* Loads under Sv39, against the same loads untranslated.
 *
 * Each case is a few loads in a fixed order from the scratchpad, the control
 * region and a cached DRAM line, into t0..t2, ending in ecall. It runs twice:
 * in supervisor mode with satp on, and in machine mode. Every mapping is the
 * identity, so both runs issue the same addresses; translation is the only
 * difference. Nothing is stored while a case runs: the machine trap handler
 * captures t0..t2 and mcause, so a difference is a load's doing.
 *
 * Bare metal, no library. Built per mesh (-DMESH=n): the DRAM a node owns
 * carries its mesh in bits 37:36. Runs on rv64_node_pair (one image per node)
 * and on the kos core models. Page tables live in DRAM, written uncached. */

#ifndef MESH
#define MESH 0
#endif

#define CTRL    0x00020000UL
#define CONSOLE ((volatile unsigned char *)(CTRL + 0x08))
#define EXIT    ((volatile unsigned long *)(CTRL + 0x00))
#define DCACHE  ((volatile unsigned long *)(CTRL + 0x1C8))
#define CTRL_LD (CTRL + 0x300) /* a readable control register */
#define DRAM    (0x80000000UL | ((unsigned long)MESH << 36))
#define UNC     (1UL << 38)

#define PT_ROOT (DRAM + 0x10000)
#define PT_LOW1 (DRAM + 0x11000) /* VA 0 .. 1 GB */
#define PT_LOW0 (DRAM + 0x12000) /* VA 0 .. 2 MB, 4 KB pages */
#define PT_HI1  (DRAM + 0x13000) /* the gigabyte holding DRAM */
#define DATA    (DRAM + 0x1000)  /* the DRAM words the cases load */

#define PTE_V 1UL
#define PTE_G 32UL
#define LEAF  (PTE_V | 2UL | 4UL | 8UL | PTE_G | 64UL | 128UL) /* RWX, A, D */

#define WARM "li t0, 0\n li t1, 0\n li t2, 0\n ld t3, 0(a3)\n ld t3, 32(a3)\n fence\n"
#define CASE(n, body)                                                                                        \
    __attribute__((aligned(4), naked)) static void case##n(void)                                            \
    {                                                                                                        \
        __asm__ volatile(WARM body "ecall\n j .\n");                                                         \
    }

/* a1 = scratchpad words, a2 = control register, a3 = DRAM words */
CASE(0, "ld t0, 0(a1)\n ld t1, 8(a1)\n")
CASE(1, "ld t0, 0(a2)\n ld t1, 0(a2)\n")
CASE(2, "ld t0, 0(a2)\n ld t1, 8(a3)\n")
CASE(3, "ld t0, 16(a1)\n ld t1, 16(a3)\n")
CASE(4, "ld t0, 24(a3)\n ld t1, 24(a1)\n")
CASE(5, "ld t0, 0(a3)\n ld t1, 0(a2)\n")
CASE(6, "ld t0, 32(a1)\n nop\n nop\n nop\n ld t1, 8(a3)\n")
CASE(7, "ld t0, 40(a1)\n addi t0, t0, 1\n ld t1, 16(a3)\n")
CASE(8, "ld t0, 48(a1)\n ld t1, 56(a1)\n ld t2, 24(a3)\n")
CASE(9, "ld t0, 0(a2)\n ld t1, 0(a1)\n")
CASE(10, "ld t0, 8(a3)\n ld t1, 16(a3)\n ld t2, 24(a3)\n")
CASE(11, "ld t0, 0(a1)\n")
/* a store after a cached load: words 1 and 2 are DATA[1] and spad_words[8]
 * as M reads them afterwards (back()) */
CASE(12, "li t1, 0x5a5a\n ld t0, 8(a3)\n sd t1, 64(a1)\n")
CASE(13, "ld t0, 8(a3)\n nop\n ld t1, 16(a3)\n")
CASE(14, "ld t0, 8(a3)\n addi t2, t2, 0\n ld t1, 16(a3)\n")
CASE(15, "li t1, 0x5a5a\n ld t0, 8(a3)\n nop\n sd t1, 64(a1)\n")
/* a load after a cached store (DATA[5] is no case's load) */
CASE(16, "li t1, 0x77\n sd t1, 40(a3)\n ld t0, 24(a3)\n")
CASE(17, "li t1, 0x77\n sd t1, 40(a3)\n nop\n ld t0, 24(a3)\n")

#define NCASE 18
#define STORE_CASE(c) ((c) == 12 || (c) == 15)
static void (*const cases[NCASE])(void) = {case0,  case1,  case2,  case3,  case4,  case5,
                                           case6,  case7,  case8,  case9,  case10, case11,
                                           case12, case13, case14, case15, case16, case17};
static const char *const names[NCASE] = {
    "spad,spad",       "ctrl,ctrl",     "ctrl,dram",      "spad,dram",           "dram,spad",
    "dram,ctrl",       "spad,3nop,dram", "spad,use,dram", "spad,spad,dram",      "ctrl,spad",
    "dram,dram,dram",  "spad",           "dram,st spad",  "dram,nop,dram",       "dram,alu,dram",
    "dram,nop,st spad", "st dram,dram",  "st dram,nop,dram",
};

unsigned long spad_words[16] = {0x5000, 0x5001, 0x5002, 0x5003, 0x5004, 0x5005, 0x5006, 0x5007};
volatile unsigned long cap[4]; /* t0, t1, t2, mcause: written by m_trap */
unsigned long got[NCASE][2][4];
volatile unsigned long step;

static void putch(char c) { *CONSOLE = (unsigned char)c; }
static void put_str(const char *s)
{
    while (*s) {
        putch(*s++);
    }
}
static void put_hex(unsigned long v)
{
    for (int i = 60; i >= 0; i -= 4) {
        unsigned d = (unsigned)((v >> i) & 0xf);
        putch((char)(d < 10 ? '0' + d : 'a' + d - 10));
    }
}
static void put_dec(unsigned long v)
{
    char b[24];
    int n = 0;
    do {
        b[n++] = (char)('0' + v % 10);
        v /= 10;
    } while (v);
    while (n) {
        putch(b[--n]);
    }
}

#define csrw(name, v) __asm__ volatile("csrw " name ", %0" ::"r"((unsigned long)(v)))

int main(void);

__attribute__((section(".text.init"), naked)) void _start(void)
{
    __asm__ volatile("la sp, __stack_top\n"
                     "la t0, __bss_start\n"
                     "la t1, __bss_end\n"
                     "1: bgeu t0, t1, 2f\n"
                     "sd zero, 0(t0)\n"
                     "addi t0, t0, 8\n"
                     "j 1b\n"
                     "2: call main\n"
                     "j .\n");
}

/* Every trap: capture t0..t2 and mcause, continue in machine mode at back(). */
__attribute__((aligned(4), naked)) void m_trap(void)
{
    __asm__ volatile("csrw mscratch, t3\n"
                     "la   t3, cap\n"
                     "sd   t0, 0(t3)\n"
                     "sd   t1, 8(t3)\n"
                     "sd   t2, 16(t3)\n"
                     "csrr t0, mcause\n"
                     "sd   t0, 24(t3)\n"
                     "la   t0, back\n"
                     "csrw mepc, t0\n"
                     "csrr t0, mstatus\n"
                     "li   t1, 0x1800\n"
                     "or   t0, t0, t1\n"
                     "csrw mstatus, t0\n"
                     "la   sp, __stack_top\n"
                     "mret\n");
}

/* launch(mode, spad, ctrl, dram, entry): mret into entry with MPP = mode. */
__attribute__((aligned(4), naked)) static void launch(void)
{
    __asm__ volatile("csrw mepc, a4\n"
                     "csrr t0, mstatus\n"
                     "li   t1, 0x1800\n"
                     "not  t1, t1\n"
                     "and  t0, t0, t1\n"
                     "slli t1, a0, 11\n"
                     "or   t0, t0, t1\n"
                     "csrw mstatus, t0\n"
                     "mret\n");
}

static void run(unsigned long k)
{
    /* even steps supervisor (1), odd machine (3) */
    unsigned long mode = (k & 1) ? 3 : 1;
    ((void (*)(unsigned long, unsigned long, unsigned long, unsigned long, unsigned long))launch)(
        mode, (unsigned long)spad_words, CTRL_LD, DATA, (unsigned long)cases[k >> 1]);
}

static void finish(void)
{
    unsigned long bad = 0;
    for (int c = 0; c < NCASE; ++c) {
        unsigned long cb = 0;
        for (int w = 0; w < 3; ++w) {
            cb += got[c][0][w] != got[c][1][w];
        }
        cb += got[c][0][3] != 9 || got[c][1][3] != 11; /* ecall from S, from M */
        bad += cb != 0;
        put_str(cb ? "DIFF " : "same ");
        put_str(names[c]);
        for (int m = 0; m < 2; ++m) {
            put_str(m ? "  M" : "  S");
            for (int w = 0; w < 3; ++w) {
                putch(' ');
                put_hex(got[c][m][w]);
            }
            put_str(" cause ");
            put_dec(got[c][m][3]);
        }
        putch('\n');
    }
    put_str("@ xlat.cases_differing ");
    put_dec(bad);
    put_str("\nxlat done\n");
    *EXIT = bad;
    for (;;) {
    }
}

void back(void)
{
    unsigned long k = step;
    for (int w = 0; w < 4; ++w) {
        got[k >> 1][k & 1][w] = cap[w];
    }
    if (STORE_CASE(k >> 1)) {
        volatile unsigned long *d = (volatile unsigned long *)DATA, *s = spad_words;
        got[k >> 1][k & 1][1] = d[1];
        got[k >> 1][k & 1][2] = s[8];
        d[1] = 0x7001;
        s[8] = 0;
    }
    step = k + 1;
    if (k + 1 == 2 * NCASE) {
        csrw("satp", 0);
        __asm__ volatile("sfence.vma" ::: "memory");
        finish();
    }
    run(k + 1);
}

static void wr_unc(unsigned long pa, unsigned long v) { *(volatile unsigned long *)(pa | UNC) = v; }
static unsigned long pte(unsigned long pa, unsigned long f) { return ((pa >> 12) << 10) | f; }

int main(void)
{
    for (unsigned long a = PT_ROOT; a < PT_ROOT + 0x4000; a += 8) {
        wr_unc(a, 0);
    }
    wr_unc(PT_ROOT + 0 * 8, pte(PT_LOW1, PTE_V));
    wr_unc(PT_ROOT + ((DRAM >> 30) & 511) * 8, pte(PT_HI1, PTE_V));
    wr_unc(PT_LOW1, pte(PT_LOW0, PTE_V));
    for (unsigned long p = 0; p <= 0x20; ++p) {
        wr_unc(PT_LOW0 + p * 8, pte(p << 12, LEAF)); /* IMEM, SPAD, CTRL */
    }
    wr_unc(PT_HI1 + ((DRAM >> 21) & 511) * 8, pte(DRAM, LEAF)); /* DRAM's first 2 MB */
    for (int i = 0; i < 8; ++i) {
        wr_unc(DATA + 8 * i, 0x7000 + i);
    }
    *DCACHE = 3; /* write back and drop: DATA is read through the cache from DRAM */
    for (int i = 0; i < 64; ++i) {
        __asm__ volatile("nop");
    }
    csrw("mtvec", (unsigned long)&m_trap);
    csrw("medeleg", 0);
    csrw("mideleg", 0);
    csrw("satp", (8UL << 60) | (PT_ROOT >> 12));
    __asm__ volatile("sfence.vma" ::: "memory");
    step = 0;
    run(0);
    return 1;
}
