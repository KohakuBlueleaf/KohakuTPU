/* The node's ka_printf (kohakuaccel/lib/stdio/stdio.c) run natively: each
 * stdin line "<kind> <format> <value>" (kind i int, u unsigned, l int64_t,
 * L uint64_t) prints one line, what ka_printf wrote for it. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <ka/lib/stdio.h>

static char out[512];
static unsigned got;

static void sink(const char *buf, unsigned n)
{
    memcpy(out + got, buf, n);
    got += n;
}

int main(void)
{
    char line[256], fmt[64], val[64];
    char kind;
    ka_set_sink(sink);
    while (fgets(line, sizeof line, stdin)) {
        if (sscanf(line, " %c %63s %63s", &kind, fmt, val) != 3) {
            continue;
        }
        got = 0;
        long long s = strtoll(val, 0, 0);
        unsigned long long u = strtoull(val, 0, 0);
        switch (kind) {
        case 'i': ka_printf(fmt, (int)s); break;
        case 'u': ka_printf(fmt, (unsigned)u); break;
        case 'l': ka_printf(fmt, (int64_t)s); break;
        default: ka_printf(fmt, (uint64_t)u); break;
        }
        ka_flush();
        out[got] = 0;
        printf("%s\n", out);
    }
    return 0;
}
