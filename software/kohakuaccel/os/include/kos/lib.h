/* Freestanding helpers every layer links its own copy of. */
#ifndef KOS_LIB_H
#define KOS_LIB_H

#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

void *memset(void *d, int c, size_t n);
void *memcpy(void *d, const void *s, size_t n);
void *memmove(void *d, const void *s, size_t n);
int memcmp(const void *a, const void *b, size_t n);
size_t strlen(const char *s);
int strcmp(const char *a, const char *b);
int strncmp(const char *a, const char *b, size_t n);
char *strncpy(char *d, const char *s, size_t n);

/* %d %i %u %x %X %c %s %p %%, with l/ll/z, width and 0 or - flags. */
int kos_vsnprintf(char *out, size_t size, const char *fmt, va_list ap);
int kos_snprintf(char *out, size_t size, const char *fmt, ...);

/* Parse a decimal or 0x number at *s, advancing it. */
uint64_t kos_strtoul(const char **s);

/* splitmix64 */
static inline uint64_t kos_mix(uint64_t *st)
{
    uint64_t z = (*st += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}

#define KOS_ALIGN_UP(x, a) (((x) + (a) - 1) & ~((uint64_t)(a) - 1))
#define KOS_ARRAY(a) (sizeof(a) / sizeof((a)[0]))

#endif
