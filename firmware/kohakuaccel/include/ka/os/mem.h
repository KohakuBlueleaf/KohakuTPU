/* The node's card-memory regions: one heap (ka/os/heap.h) per region number,
 * each covering what ka_mem_configure was told. */
#ifndef KA_OS_MEM_H
#define KA_OS_MEM_H

#include <stdint.h>

#include <ka/os/heap.h>

#define KA_MEM_REGIONS 4
#define KA_MEM_BLOCKS  64 /* table entries per region: 31 live blocks at worst */

enum { KA_MEM_DRAM = 0, KA_MEM_STAGING = 1 };

/* (Re)cover `region` with [base, base + bytes); bytes 0 retires it. 0, or
 * KA_ST_BAD_ARG, or KA_ST_HEAP_BUSY while it has live blocks. */
int ka_mem_configure(unsigned region, uint64_t base, uint64_t bytes, uint64_t granule);

/* The region's heap, or 0 when it is not configured. */
struct ka_heap *ka_mem_heap(unsigned region);

/* ka_heap_alloc / ka_heap_free on `region`; KA_ST_BAD_ARG for an unconfigured one. */
int ka_mem_alloc(unsigned region, uint64_t bytes, uint64_t align, uint32_t tag, uint64_t *addr);
int ka_mem_free(unsigned region, uint64_t addr);

#endif
