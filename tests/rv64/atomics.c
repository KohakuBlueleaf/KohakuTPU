/* RV64A on SysCore: every AMO, both widths, plus LR/SC.
 *
 * Written against inline assembly rather than <stdatomic.h> so the encoding
 * under test is the one named here -- a C11 atomic is free to become a libcall
 * or a fence, and then the test proves nothing about the A extension.
 */

#define CONSOLE ((volatile unsigned char *)0x10000000ull)

static void putch(char c) { *CONSOLE = (unsigned char)c; }
static void puts_(const char *s) { while (*s) putch(*s++); }

static void put_u64(unsigned long long v) {
    char b[21];
    int n = 0;
    if (!v) { putch('0'); return; }
    while (v) { b[n++] = (char)('0' + (v % 10)); v /= 10; }
    while (n) putch(b[--n]);
}

static long long word_mem;
static int half_mem;
static int fails;

static void expect(const char *what, unsigned long long got,
                   unsigned long long want) {
    if (got != want) {
        ++fails;
        puts_("  FAIL ");
        puts_(what);
        puts_(" got ");
        put_u64(got);
        puts_(" want ");
        put_u64(want);
        putch('\n');
    }
}

#define AMO_D(name, op)                                                     \
    static long long name(volatile long long *p, long long v) {             \
        long long r;                                                        \
        __asm__ volatile(op " %0, %2, (%1)" : "=r"(r) : "r"(p), "r"(v)      \
                         : "memory");                                       \
        return r;                                                           \
    }

AMO_D(amoswap_d, "amoswap.d")
AMO_D(amoadd_d, "amoadd.d")
AMO_D(amoxor_d, "amoxor.d")
AMO_D(amoand_d, "amoand.d")
AMO_D(amoor_d, "amoor.d")
AMO_D(amomin_d, "amomin.d")
AMO_D(amomax_d, "amomax.d")
AMO_D(amominu_d, "amominu.d")
AMO_D(amomaxu_d, "amomaxu.d")

static int amoadd_w(volatile int *p, int v) {
    int r;
    __asm__ volatile("amoadd.w %0, %2, (%1)" : "=r"(r) : "r"(p), "r"(v)
                     : "memory");
    return r;
}

static int amomax_w(volatile int *p, int v) {
    int r;
    __asm__ volatile("amomax.w %0, %2, (%1)" : "=r"(r) : "r"(p), "r"(v)
                     : "memory");
    return r;
}

/* A compare-and-swap built the way a real runtime would: LR, compare, SC. */
static int cas_d(volatile long long *p, long long expect_v, long long newv) {
    long long tmp;
    int fail;
    __asm__ volatile(
        "1: lr.d %0, (%2)\n"
        "   bne  %0, %3, 2f\n"
        "   sc.d %1, %4, (%2)\n"
        "   bnez %1, 1b\n"
        "   li   %1, 0\n"
        "   j    3f\n"
        "2: li   %1, 1\n"
        "3:\n"
        : "=&r"(tmp), "=&r"(fail)
        : "r"(p), "r"(expect_v), "r"(newv)
        : "memory");
    return fail;
}

int main(void) {
    puts_("RV64A\n");

    word_mem = 100;
    expect("amoswap.d old", (unsigned long long)amoswap_d(&word_mem, 7), 100);
    expect("amoswap.d new", (unsigned long long)word_mem, 7);

    word_mem = 40;
    expect("amoadd.d old", (unsigned long long)amoadd_d(&word_mem, 2), 40);
    expect("amoadd.d new", (unsigned long long)word_mem, 42);

    word_mem = 0xf0f0;
    expect("amoxor.d", (unsigned long long)amoxor_d(&word_mem, 0xff), 0xf0f0);
    expect("amoxor.d new", (unsigned long long)word_mem, 0xf00f);

    word_mem = 0xff00;
    amoand_d(&word_mem, 0x0ff0);
    expect("amoand.d", (unsigned long long)word_mem, 0x0f00);

    word_mem = 0x00f0;
    amoor_d(&word_mem, 0x0f00);
    expect("amoor.d", (unsigned long long)word_mem, 0x0ff0);

    /* Signed against unsigned on the same bits: -1 is the smallest signed and
       the largest unsigned, so these two must disagree. */
    word_mem = -1;
    amomin_d(&word_mem, 5);
    expect("amomin.d signed", (unsigned long long)word_mem, (unsigned long long)-1LL);

    word_mem = -1;
    amomax_d(&word_mem, 5);
    expect("amomax.d signed", (unsigned long long)word_mem, 5);

    word_mem = -1;
    amominu_d(&word_mem, 5);
    expect("amominu.d", (unsigned long long)word_mem, 5);

    word_mem = -1;
    amomaxu_d(&word_mem, 5);
    expect("amomaxu.d", (unsigned long long)word_mem, (unsigned long long)-1LL);

    /* The W forms sign-extend their RESULT from bit 31. */
    half_mem = 0x7ffffffe;
    expect("amoadd.w old", (unsigned long long)(long long)amoadd_w(&half_mem, 1),
           0x7ffffffe);
    expect("amoadd.w new", (unsigned long long)(long long)half_mem, 0x7fffffff);

    half_mem = -5;
    expect("amomax.w old", (unsigned long long)(long long)amomax_w(&half_mem, 3),
           (unsigned long long)-5LL);
    expect("amomax.w new", (unsigned long long)(long long)half_mem, 3);

    /* LR/SC: the success path, then a stale expectation that must fail. */
    word_mem = 1234;
    expect("cas success", (unsigned long long)cas_d(&word_mem, 1234, 5678), 0);
    expect("cas stored", (unsigned long long)word_mem, 5678);
    expect("cas mismatch", (unsigned long long)cas_d(&word_mem, 1234, 9999), 1);
    expect("cas unchanged", (unsigned long long)word_mem, 5678);

    /* A lock-free counter, the way a runtime uses one. */
    word_mem = 0;
    for (int i = 0; i < 1000; ++i) amoadd_d(&word_mem, 3);
    expect("counter", (unsigned long long)word_mem, 3000);

    puts_(fails ? "ATOMICS FAIL\n" : "atomics ok\n");
    return fails;
}
