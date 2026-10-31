/* The firmware's string routines natively, on a ring of bytes: each stdin line
 * "<op> a b n" answers with a hash of the buffer and the routine's result. */
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#include <ka/lib/string.h>

#define SIZE 256

static unsigned char buf[SIZE];

static uint64_t hash(void)
{
    uint64_t h = 0xcbf29ce484222325UL;
    for (unsigned i = 0; i < SIZE; ++i) {
        h = (h ^ buf[i]) * 0x100000001b3UL;
    }
    return h;
}

int main(void)
{
    char op;
    unsigned a, b, n;
    while (scanf(" %c %u %u %u", &op, &a, &b, &n) == 4) {
        for (unsigned i = 0; i < SIZE; ++i) {
            buf[i] = (unsigned char)(i * 7 + 3);
        }
        int r = 0;
        switch (op) {
        case 's': memset(buf + a, (int)b, n); break;
        case 'c': memcpy(buf + a, buf + b, n); break;
        case 'm': memmove(buf + a, buf + b, n); break;
        case 'x': r = memcmp(buf + a, buf + b, n); break;
        default: buf[a + n] = 0; r = (int)strlen((char *)buf + a); break;
        }
        printf("%llu %d\n", (unsigned long long)hash(), r);
    }
    return 0;
}
