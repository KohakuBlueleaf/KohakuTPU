/* stdio: a 64-byte line buffer in front of the sinks, and a small printf. */
#include <ka/hal/node.h>
#include <ka/lib/stdio.h>

static ka_sink_fn sink;
static ka_source_fn source;
static char line[64];
static unsigned fill;

void ka_set_sink(ka_sink_fn fn) { sink = fn; }
void ka_set_source(ka_source_fn fn) { source = fn; }

void ka_flush(void)
{
    if (fill && sink) {
        sink(line, fill);
    }
    fill = 0;
}

void ka_putc(char c)
{
    if (!sink) {
        ka_ctrl_wr(KA_R_CONSOLE, (unsigned char)c);
        return;
    }
    line[fill++] = c;
    if (c == '\n' || fill == sizeof line) {
        ka_flush();
    }
}

void ka_debug(const char *s)
{
    while (*s) {
        ka_ctrl_wr(KA_R_CONSOLE, (unsigned char)*s++);
    }
}

void ka_puts(const char *s)
{
    while (*s) {
        ka_putc(*s++);
    }
}

int ka_getc_nb(void)
{
    if (source) {
        return source();
    }
    uint64_t v = ka_ctrl_rd(KA_R_STDIN);
    if (!(v & 0x100u)) {
        return -1;
    }
    ka_ctrl_wr(KA_R_STDIN, 0);
    return (int)(v & 0xffu);
}

int ka_getc(void)
{
    int c;
    ka_flush();
    while ((c = ka_getc_nb()) < 0) {
    }
    return c;
}

/* Digits of `v` in `base`, right-aligned in `width` with `pad`. */
static int put_num(uint64_t v, unsigned base, int width, char pad, int neg)
{
    char buf[24];
    int n = 0, out = 0;

    /* Hex by shifts: a divide costs 66 cycles. */
    do {
        unsigned d = base == 16 ? (unsigned)(v & 15) : (unsigned)(v % base);
        buf[n++] = (char)(d < 10 ? '0' + d : 'a' + d - 10);
        v = base == 16 ? v >> 4 : v / base;
    } while (v);
    if (neg) {
        buf[n++] = '-';
    }
    for (; width > n; --width, ++out) {
        ka_putc(pad);
    }
    while (n) {
        ka_putc(buf[--n]);
        ++out;
    }
    return out;
}

int ka_vprintf(const char *fmt, va_list ap)
{
    int out = 0;
    for (; *fmt; ++fmt) {
        if (*fmt != '%') {
            ka_putc(*fmt);
            ++out;
            continue;
        }
        ++fmt;
        char pad = ' ';
        int width = 0, lng = 0;
        if (*fmt == '0') {
            pad = '0';
            ++fmt;
        }
        while (*fmt >= '0' && *fmt <= '9') {
            width = width * 10 + (*fmt++ - '0');
        }
        while (*fmt == 'l') {
            lng = 1;
            ++fmt;
        }
        switch (*fmt) {
        case 'd': {
            int64_t v = lng ? va_arg(ap, int64_t) : va_arg(ap, int);
            out += put_num(v < 0 ? (uint64_t)-v : (uint64_t)v, 10, width, pad, v < 0);
            break;
        }
        case 'u':
            out += put_num(lng ? va_arg(ap, uint64_t) : va_arg(ap, unsigned), 10, width,
                           pad, 0);
            break;
        case 'x':
            out += put_num(lng ? va_arg(ap, uint64_t) : va_arg(ap, unsigned), 16, width,
                           pad, 0);
            break;
        case 'p':
            ka_puts("0x");
            out += 2 + put_num((uint64_t)va_arg(ap, void *), 16, width, pad, 0);
            break;
        case 's': {
            const char *s = va_arg(ap, const char *);
            while (*s) {
                ka_putc(*s++);
                ++out;
            }
            break;
        }
        case 'c':
            ka_putc((char)va_arg(ap, int));
            ++out;
            break;
        case '%':
            ka_putc('%');
            ++out;
            break;
        default:
            return out;
        }
    }
    return out;
}

int ka_printf(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = ka_vprintf(fmt, ap);
    va_end(ap);
    return n;
}
