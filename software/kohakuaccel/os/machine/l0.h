/* L0, the machine layer: internal interfaces. */
#ifndef KOS_L0_H
#define KOS_L0_H

#include <kos/abi.h>
#include <kos/boot.h>
#include <kos/hw.h>

/* x1..x31 at index 1..31; the interrupted sp at 2. */
struct mframe {
    uint64_t x[32];
};

/* The regions this node's L0 serves, from the boot table. */
struct l0_mem {
    uint64_t priv, priv_size;   /* own private DRAM */
    uint64_t pool, pool_pages;  /* the page-table pool (staging or private DRAM tail) */
    int pool_dram;
    uint64_t shared, shared_size;
    uint64_t staging[KOS_MAX_NODES], staging_size[KOS_MAX_NODES]; /* by mesh id */
};

extern struct l0_mem l0_mem;
extern volatile struct kos_boot *const l0_boot;

void mmu_init(void);
int mmu_map(uint64_t va, uint64_t pa, uint64_t size, unsigned attr, int checked);
int mmu_unmap(uint64_t va, uint64_t size);
uint64_t mmu_query(uint64_t va);
uint64_t mmu_satp(void);
uint64_t mmu_tables(void);
uint64_t mmu_maps(void);

void l0_puts(const char *s);
void l0_say(const char *fmt, ...);
__attribute__((noreturn)) void l0_halt(uint64_t code);

#endif
