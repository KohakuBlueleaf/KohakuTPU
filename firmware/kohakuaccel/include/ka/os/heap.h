/* Region heaps: one span of unit-global memory handed out in aligned blocks,
 * first fit by address, with the block table in the caller's array. */
#ifndef KA_OS_HEAP_H
#define KA_OS_HEAP_H

#include <stdint.h>

/* One span of the heap, used or free. The table covers the heap exactly, in
 * address order. */
struct ka_block {
    uint64_t off; /* from the heap's base */
    uint64_t len;
    uint32_t used;
    uint32_t tag; /* the allocator's caller's, returned by ka_heap_find */
};

struct ka_heap {
    uint64_t base;    /* unit-global; a multiple of granule */
    uint64_t bytes;   /* whole granules */
    uint64_t granule; /* power of two >= 8: every block is whole granules */
    struct ka_block *b;
    uint32_t cap; /* entries in b */
    uint32_t n;   /* entries in use */
    uint64_t used, peak;
    uint32_t live, fails;
};

struct ka_heap_stats {
    uint64_t free, largest, used, peak;
    uint32_t live, blocks, fails;
};

/* 0, or KA_ST_BAD_ARG (granule not a power of two >= 8, base not aligned to
 * it, fewer than one granule, a table under 3 entries). */
int ka_heap_init(struct ka_heap *h, uint64_t base, uint64_t bytes, uint64_t granule,
                 struct ka_block *table, uint32_t cap);

/* `bytes` rounded up to the granule, at an address aligned to max(align,
 * granule). 0 and *addr, or KA_ST_BAD_ARG (zero size, align not a power of
 * two), KA_ST_NO_MEMORY (no free block holds it), KA_ST_NO_SLOTS (the first
 * block that holds it needs more table entries than are left). */
int ka_heap_alloc(struct ka_heap *h, uint64_t bytes, uint64_t align, uint32_t tag,
                  uint64_t *addr);

/* 0, or KA_ST_BAD_FREE (not the start of a used block). */
int ka_heap_free(struct ka_heap *h, uint64_t addr);

/* The used block that starts at `addr`, or 0. */
const struct ka_block *ka_heap_find(const struct ka_heap *h, uint64_t addr);

void ka_heap_stats(const struct ka_heap *h, struct ka_heap_stats *s);

/* 0 when the table covers the heap exactly, in order, granule-aligned, with no
 * two free neighbours and `used`/`live` matching; else the first bad entry + 1. */
uint32_t ka_heap_check(const struct ka_heap *h);

#endif
