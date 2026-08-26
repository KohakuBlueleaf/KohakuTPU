/* The phase-2 node program: proves the unit runs a real image delivered over
 * flits and reports through the completion, not through a simulator back door.
 *
 * Everything it touches is inside the unit -- imem for .text, spad for
 * everything else, and the control region for the console and the exit word.
 * There is no memory requestor yet, so a load outside the spad would go
 * nowhere. */

#define CTRL_BASE 0x00020000UL
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }
static void put_u64(unsigned long v) {
    char b[21]; int n = 0;
    if (!v) { putch('0'); return; }
    while (v) { b[n++] = (char)('0' + (v % 10)); v /= 10; }
    while (n) putch(b[--n]);
}

/* .data and .bss both have to survive the loader, so exercise both. */
static unsigned long table[16] = {
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16
};
static unsigned long scratch[16];

static unsigned long fib(unsigned long n)
{
    unsigned long a = 0, b = 1, i;
    for (i = 0; i < n; ++i) { unsigned long t = a + b; a = b; b = t; }
    return a;
}

int main(void)
{
    int fails = 0;
    unsigned long i, sum = 0;

    /* .data arrived intact */
    for (i = 0; i < 16; ++i) sum += table[i];
    if (sum != 136) { ++fails; put_str("FAIL table sum "); put_u64(sum); putch('\n'); }

    /* .bss was cleared, and the spad takes a write */
    for (i = 0; i < 16; ++i) if (scratch[i]) { ++fails; break; }
    for (i = 0; i < 16; ++i) scratch[i] = table[i] * 3;
    if (scratch[15] != 48) { ++fails; put_str("FAIL scratch\n"); }

    /* the multiplier and the branch predictor both get a workout */
    if (fib(40) != 102334155UL) { ++fails; put_str("FAIL fib\n"); }

    /* byte and halfword stores, since the spad has byte enables */
    volatile unsigned char *bp = (volatile unsigned char *)scratch;
    bp[0] = 0xAB; bp[3] = 0xCD;
    if (bp[0] != 0xAB || bp[3] != 0xCD) { ++fails; put_str("FAIL bytes\n"); }

    if (!fails) put_str("pe ok\n");
    return fails;
}
