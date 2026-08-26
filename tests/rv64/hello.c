/* A first serious program for SysCore: not a directed test, a workload.
 *
 * It exercises the paths a test case does not -- a call chain with a stack,
 * 64-bit multiply-free arithmetic, byte and halfword memory traffic, a sort
 * over an array, and enough loop iterations that a wrong forward or a lost
 * branch shows up as a wrong answer rather than a lucky pass.
 *
 * No libc: the toolchain's would pull in code this core cannot run yet.
 */

#define CONSOLE ((volatile unsigned char *)0x10000000ull)

static void putch(char c) { *CONSOLE = (unsigned char)c; }

static void puts_(const char *s) {
    while (*s) putch(*s++);
}

static void put_u64(unsigned long long v) {
    char b[21];
    int n = 0;
    if (!v) {
        putch('0');
        return;
    }
    while (v) {
        b[n++] = (char)('0' + (v % 10));
        v /= 10;
    }
    while (n) putch(b[--n]);
}

/* A 64-bit LCG: exercises wide arithmetic without needing M. */
static unsigned long long seed = 0x0123456789abcdefULL;
static unsigned long long rnd(void) {
    seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
    return seed >> 16;
}

#define N 256
static unsigned int a[N];
static unsigned char bytes[N];
static unsigned short halves[N];

static void isort(unsigned int *v, int n) {
    for (int i = 1; i < n; ++i) {
        unsigned int k = v[i];
        int j = i - 1;
        while (j >= 0 && v[j] > k) {
            v[j + 1] = v[j];
            --j;
        }
        v[j + 1] = k;
    }
}

static unsigned long long checksum(void) {
    unsigned long long s = 0;
    for (int i = 0; i < N; ++i) {
        s += a[i];
        s ^= (unsigned long long)bytes[i] << (i & 31);
        s += (unsigned long long)halves[i] * 3ULL;
        s = (s << 7) | (s >> 57);
    }
    return s;
}

/* Recursion, so the stack and the call chain are real. */
static unsigned long long fib(int n) {
    return n < 2 ? (unsigned long long)n : fib(n - 1) + fib(n - 2);
}

int main(void) {
    puts_("SysCore alive\n");

    for (int i = 0; i < N; ++i) {
        unsigned long long r = rnd();
        a[i] = (unsigned int)r;
        bytes[i] = (unsigned char)(r >> 8);
        halves[i] = (unsigned short)(r >> 16);
    }

    isort(a, N);

    int ordered = 1;
    for (int i = 1; i < N; ++i)
        if (a[i - 1] > a[i]) ordered = 0;

    puts_("sorted: ");
    puts_(ordered ? "yes" : "NO");
    putch('\n');

    puts_("checksum: ");
    put_u64(checksum());
    putch('\n');

    puts_("fib(21): ");
    unsigned long long f = fib(21);
    put_u64(f);
    putch('\n');

    /* Signed and unsigned compares, shifts across the word boundary. */
    long long sacc = 0;
    unsigned long long uacc = 0;
    for (int i = 0; i < 4096; ++i) {
        long long x = (long long)rnd();
        sacc += (x < 0) ? -x : x;
        uacc += ((unsigned long long)x) >> (i & 63);
        uacc ^= ((unsigned long long)x) << (i & 63);
    }
    puts_("acc: ");
    put_u64((unsigned long long)sacc);
    putch(' ');
    put_u64(uacc);
    putch('\n');

    if (!ordered) return 1;
    if (f != 10946ULL) return 2;
    return 0;
}
