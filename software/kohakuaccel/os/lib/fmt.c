#include <kos/lib.h>

struct out {
    char *p;
    size_t size, n;
};

#ifdef KOS_SMALL_CALL
#define FMT_COLD __attribute__((minsize))
#else
#define FMT_COLD
#endif

static FMT_COLD void put(struct out *o, char c)
{
    if (o->n + 1 < o->size) {
        o->p[o->n] = c;
    }
    ++o->n;
}

static FMT_COLD void num(struct out *o, uint64_t v, unsigned base, int neg, int width, char pad, int left,
                int upper)
{
    char buf[24];
    int n = 0;
    const char *dig = upper ? "0123456789ABCDEF" : "0123456789abcdef";
    do {
        buf[n++] = dig[v % base];
        v /= base;
    } while (v);
    int len = n + neg;
    if (neg && pad == '0') {
        put(o, '-');
    }
    if (!left) {
        for (int i = len; i < width; ++i) {
            put(o, pad);
        }
    }
    if (neg && pad != '0') {
        put(o, '-');
    }
    while (n) {
        put(o, buf[--n]);
    }
    if (left) {
        for (int i = len; i < width; ++i) {
            put(o, ' ');
        }
    }
}

FMT_COLD int kos_vsnprintf(char *out, size_t size, const char *fmt, va_list ap)
{
    struct out o = {out, size, 0};
    for (; *fmt; ++fmt) {
        if (*fmt != '%') {
            put(&o, *fmt);
            continue;
        }
        ++fmt;
        int left = 0, width = 0, lng = 0;
        char pad = ' ';
        if (*fmt == '-') {
            left = 1;
            ++fmt;
        }
        if (*fmt == '0') {
            pad = '0';
            ++fmt;
        }
        while (*fmt >= '0' && *fmt <= '9') {
            width = width * 10 + (*fmt++ - '0');
        }
        while (*fmt == 'l' || *fmt == 'z') {
            lng = 1;
            ++fmt;
        }
        switch (*fmt) {
        case 'd':
        case 'i': {
            int64_t v = lng ? va_arg(ap, int64_t) : va_arg(ap, int);
            num(&o, v < 0 ? (uint64_t)-v : (uint64_t)v, 10, v < 0, width, pad, left, 0);
            break;
        }
        case 'u':
            num(&o, lng ? va_arg(ap, uint64_t) : va_arg(ap, unsigned), 10, 0, width, pad, left, 0);
            break;
        case 'x':
        case 'X':
            num(&o, lng ? va_arg(ap, uint64_t) : va_arg(ap, unsigned), 16, 0, width, pad, left,
                *fmt == 'X');
            break;
        case 'p':
            put(&o, '0');
            put(&o, 'x');
            num(&o, (uint64_t)va_arg(ap, void *), 16, 0, width, pad, left, 0);
            break;
        case 'c':
            put(&o, (char)va_arg(ap, int));
            break;
        case 's': {
            const char *s = va_arg(ap, const char *);
            int len = (int)strlen(s);
            if (!left) {
                for (int i = len; i < width; ++i) {
                    put(&o, ' ');
                }
            }
            while (*s) {
                put(&o, *s++);
            }
            if (left) {
                for (int i = len; i < width; ++i) {
                    put(&o, ' ');
                }
            }
            break;
        }
        case '%':
            put(&o, '%');
            break;
        default:
            put(&o, '%');
            put(&o, *fmt);
            break;
        }
    }
    if (size) {
        out[o.n < size ? o.n : size - 1] = 0;
    }
    return (int)o.n;
}

int kos_snprintf(char *out, size_t size, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int n = kos_vsnprintf(out, size, fmt, ap);
    va_end(ap);
    return n;
}
