#include <kos/lib.h>

void *memset(void *d, int c, size_t n)
{
    unsigned char *p = d;
    if (!((uintptr_t)p & 7) && !(n & 7)) {
        uint64_t v = (unsigned char)c * 0x0101010101010101ULL;
        for (size_t i = 0; i < n; i += 8) {
            *(uint64_t *)(p + i) = v;
        }
        return d;
    }
    while (n--) {
        *p++ = (unsigned char)c;
    }
    return d;
}

void *memcpy(void *d, const void *s, size_t n)
{
    unsigned char *a = d;
    const unsigned char *b = s;
    if (!(((uintptr_t)a | (uintptr_t)b | n) & 7)) {
        for (size_t i = 0; i < n; i += 8) {
            *(uint64_t *)(a + i) = *(const uint64_t *)(b + i);
        }
        return d;
    }
    while (n--) {
        *a++ = *b++;
    }
    return d;
}

void *memmove(void *d, const void *s, size_t n)
{
    unsigned char *a = d;
    const unsigned char *b = s;
    if (a < b) {
        while (n--) {
            *a++ = *b++;
        }
    } else {
        while (n--) {
            a[n] = b[n];
        }
    }
    return d;
}

int memcmp(const void *a, const void *b, size_t n)
{
    const unsigned char *x = a, *y = b;
    for (size_t i = 0; i < n; ++i) {
        if (x[i] != y[i]) {
            return x[i] - y[i];
        }
    }
    return 0;
}

size_t strlen(const char *s)
{
    size_t n = 0;
    while (s[n]) {
        ++n;
    }
    return n;
}

int strcmp(const char *a, const char *b)
{
    while (*a && *a == *b) {
        ++a;
        ++b;
    }
    return (unsigned char)*a - (unsigned char)*b;
}

int strncmp(const char *a, const char *b, size_t n)
{
    for (size_t i = 0; i < n; ++i) {
        if (a[i] != b[i] || !a[i]) {
            return (unsigned char)a[i] - (unsigned char)b[i];
        }
    }
    return 0;
}

char *strncpy(char *d, const char *s, size_t n)
{
    size_t i = 0;
    for (; i < n && s[i]; ++i) {
        d[i] = s[i];
    }
    for (; i < n; ++i) {
        d[i] = 0;
    }
    return d;
}

uint64_t kos_strtoul(const char **s)
{
    const char *p = *s;
    uint64_t v = 0;
    while (*p == ' ') {
        ++p;
    }
    if (p[0] == '0' && (p[1] == 'x' || p[1] == 'X')) {
        p += 2;
        for (;; ++p) {
            unsigned d;
            if (*p >= '0' && *p <= '9') {
                d = *p - '0';
            } else if (*p >= 'a' && *p <= 'f') {
                d = *p - 'a' + 10;
            } else if (*p >= 'A' && *p <= 'F') {
                d = *p - 'A' + 10;
            } else {
                break;
            }
            v = v * 16 + d;
        }
    } else {
        while (*p >= '0' && *p <= '9') {
            v = v * 10 + (uint64_t)(*p++ - '0');
        }
    }
    *s = p;
    return v;
}
