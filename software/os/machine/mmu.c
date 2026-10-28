/* The soft MMU: L0 owns this node's Sv39 tables. The tables live where the
 * walker reads them without the L1 (staging, or private DRAM written through
 * the uncached alias), every change is followed by sfence.vma, and a mapping
 * the kernel asks for is checked against the memory the boot table gave it. */
#include <kos/lib.h>

#include "l0.h"

#define PTE_V 1UL
#define PTE_R 2UL
#define PTE_W 4UL
#define PTE_X 8UL
#define PTE_U 16UL
#define PTE_G 32UL
#define PTE_A 64UL
#define PTE_D 128UL
#define PTE_LEAF (PTE_R | PTE_W | PTE_X)

uint64_t l0_root, l0_table_alias;
#define root l0_root
static uint64_t used, maps;
static uint8_t inuse[64];

static volatile uint64_t *table(uint64_t pa)
{
    return (volatile uint64_t *)(l0_mem.pool_dram ? (pa | HW_UNC) : pa);
}

static uint64_t pte_pa(uint64_t pte) { return ((pte >> 10) << 12) & ((1UL << 40) - 1); }

static uint64_t alloc_table(void)
{
    for (uint64_t i = 0; i < l0_mem.pool_pages; ++i) {
        if (!inuse[i]) {
            inuse[i] = 1;
            ++used;
            uint64_t pa = l0_mem.pool + (i << 12);
            volatile uint64_t *t = table(pa);
            for (int k = 0; k < 512; ++k) {
                t[k] = 0;
            }
            return pa;
        }
    }
    return 0;
}

void mmu_init(void)
{
    for (unsigned i = 0; i < sizeof inuse; ++i) {
        inuse[i] = 0;
    }
    used = 0;
    root = alloc_table();
    l0_table_alias = l0_mem.pool_dram ? HW_UNC : 0;
}

uint64_t mmu_satp(void) { return (8UL << 60) | (root >> 12); }
uint64_t mmu_tables(void) { return used; }
uint64_t mmu_maps(void) { return maps; }

static int canonical(uint64_t va)
{
    uint64_t top = va >> 38;
    return top == 0 || top == (~0UL >> 38);
}

/* The physical ranges a kernel request may name. */
static int allowed(uint64_t pa, uint64_t size)
{
    uint64_t end = pa + size;
    if (end < pa) {
        return 0;
    }
    if (l0_mem.pool_dram && pa < l0_mem.pool + KOS_STG_PTSIZE && end > l0_mem.pool) {
        return 0; /* the page tables themselves */
    }
    if (pa >= l0_mem.priv && end <= l0_mem.priv + l0_mem.priv_size) {
        return 1;
    }
    if (l0_mem.shared_size && pa >= l0_mem.shared && end <= l0_mem.shared + l0_mem.shared_size) {
        return 1;
    }
    for (unsigned m = 0; m < KOS_MAX_NODES; ++m) {
        uint64_t s = l0_mem.staging[m];
        if (l0_mem.staging_size[m] && pa >= s + KOS_STG_MSG && end <= s + l0_mem.staging_size[m]) {
            return 1;
        }
    }
    return 0;
}

static int is_dram(uint64_t pa) { return !(pa & HW_SPECIAL) && pa >= HW_NODE_BASE; }

/* The slot for `va` at `level` (2 root, 0 leaf), building tables on the way. */
static volatile uint64_t *slot(uint64_t va, int level, int create)
{
    uint64_t t = root;
    for (int l = 2; l > level; --l) {
        volatile uint64_t *e = table(t) + ((va >> (12 + 9 * l)) & 511);
        uint64_t v = *e;
        if (!(v & PTE_V)) {
            if (!create) {
                return 0;
            }
            uint64_t n = alloc_table();
            if (!n) {
                return 0;
            }
            *e = ((n >> 12) << 10) | PTE_V;
            v = *e;
        } else if (v & PTE_LEAF) {
            return 0; /* a superpage is in the way */
        }
        t = pte_pa(v);
    }
    return table(t) + ((va >> (12 + 9 * level)) & 511);
}

int mmu_map(uint64_t va, uint64_t pa, uint64_t size, unsigned attr, int checked)
{
    if ((va | pa | size) & 0xfff || !size || !canonical(va) || !canonical(va + size - 1)) {
        return KOS_EINVAL;
    }
    if (checked && (va < 0x1000000UL || !allowed(pa, size))) {
        return KOS_EPERM;
    }
    uint64_t flags = PTE_V | PTE_A | PTE_D;
    flags |= (attr & KOS_MAP_R) ? PTE_R : 0;
    flags |= (attr & KOS_MAP_W) ? PTE_W : 0;
    flags |= (attr & KOS_MAP_X) ? PTE_X : 0;
    flags |= (attr & KOS_MAP_U) ? PTE_U : PTE_G;
    if (!(flags & PTE_LEAF)) {
        return KOS_EINVAL;
    }
    uint64_t phys = pa;
    if ((attr & KOS_MAP_UNCACHED) && is_dram(pa)) {
        phys |= HW_UNC;
    }
    while (size) {
        int level = 0;
        if (!((va | phys) & 0x3FFFFFFF) && size >= 0x40000000) {
            level = 2;
        } else if (!((va | phys) & 0x1FFFFF) && size >= 0x200000) {
            level = 1;
        }
        volatile uint64_t *e = slot(va, level, 1);
        if (!e) {
            return KOS_ENOMEM;
        }
        if (*e & PTE_V) {
            return KOS_EBUSY;
        }
        *e = ((phys >> 12) << 10) | flags;
        uint64_t step = 1UL << (12 + 9 * level);
        va += step;
        phys += step;
        size -= step;
    }
    ++maps;
    __asm__ volatile("sfence.vma" ::: "memory");
    return KOS_OK;
}

static volatile uint64_t *leaf(uint64_t va, int *level)
{
    uint64_t t = root;
    for (int l = 2; l >= 0; --l) {
        volatile uint64_t *e = table(t) + ((va >> (12 + 9 * l)) & 511);
        uint64_t v = *e;
        if (!(v & PTE_V)) {
            return 0;
        }
        if (v & PTE_LEAF) {
            *level = l;
            return e;
        }
        t = pte_pa(v);
    }
    return 0;
}

int mmu_unmap(uint64_t va, uint64_t size)
{
    if ((va | size) & 0xfff || va < 0x1000000UL || !canonical(va)) {
        return KOS_EINVAL;
    }
    while (size) {
        int level = 0;
        volatile uint64_t *e = leaf(va, &level);
        if (!e) {
            return KOS_ENOENT;
        }
        uint64_t step = 1UL << (12 + 9 * level);
        if ((va & (step - 1)) || size < step) {
            return KOS_EINVAL; /* part of a superpage */
        }
        *e = 0;
        va += step;
        size -= step;
    }
    __asm__ volatile("sfence.vma" ::: "memory");
    return KOS_OK;
}

/* The leaf PTE of va; inside a superpage, with the page number of va's 4 KB
 * page (the leaf's own page number plus va's page within the leaf). */
#ifndef KOS_L0_FAST
uint64_t mmu_query(uint64_t va)
{
    int level = 0;
    volatile uint64_t *e = leaf(va, &level);
    if (!e) {
        return 0;
    }
#if defined(KDS_MUT) && KDS_MUT == 8
    return *e; /* mutant: the leaf's own page number (tests/host/test_kds.py) */
#endif
    return *e + (((va >> 12) & ((1UL << (9 * level)) - 1)) << 10);
}
#endif
