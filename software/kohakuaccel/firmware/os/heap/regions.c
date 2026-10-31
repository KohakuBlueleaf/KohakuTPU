/* The node's region table (ka/os/mem.h): 4 regions x 64 entries x 24 B = 6 KB of SPAD. */
#include <ka/hal/cpu.h>
#include <ka/os/mem.h>
#include <ka/package/status.h>

/* Written whole by ka_heap_init before use; only `on` must start zero. */
static KA_NOINIT struct ka_heap heaps[KA_MEM_REGIONS];
static KA_NOINIT struct ka_block tables[KA_MEM_REGIONS][KA_MEM_BLOCKS];
static uint8_t on[KA_MEM_REGIONS];

int ka_mem_configure(unsigned region, uint64_t base, uint64_t bytes, uint64_t granule)
{
    if (region >= KA_MEM_REGIONS) {
        return KA_ST_BAD_ARG;
    }
    if (on[region] && heaps[region].live) {
        return KA_ST_HEAP_BUSY;
    }
    if (!bytes) {
        on[region] = 0;
        return 0;
    }
    int rc = ka_heap_init(&heaps[region], base, bytes, granule, tables[region], KA_MEM_BLOCKS);
    on[region] = rc == 0;
    return rc;
}

struct ka_heap *ka_mem_heap(unsigned region)
{
    return (region < KA_MEM_REGIONS && on[region]) ? &heaps[region] : 0;
}

int ka_mem_alloc(unsigned region, uint64_t bytes, uint64_t align, uint32_t tag, uint64_t *addr)
{
    struct ka_heap *h = ka_mem_heap(region);
    return h ? ka_heap_alloc(h, bytes, align, tag, addr) : KA_ST_BAD_ARG;
}

int ka_mem_free(unsigned region, uint64_t addr)
{
    struct ka_heap *h = ka_mem_heap(region);
    return h ? ka_heap_free(h, addr) : KA_ST_BAD_ARG;
}
